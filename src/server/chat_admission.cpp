// Slot ownership and bounded waiting, independent of model execution.
#include "chat_server_internal.h"

namespace q4t::server {

int ChatServer::AllocSeqId(RequestControl* control, int fd, size_t required) {
  std::unique_lock<std::mutex> lock(seq_mu_);
  // Destruction order: unlink ticket, notify successors, then unlock. Every
  // return/exception removes exactly this waiter without allocating storage.
  ScopeExit notify([&] { seq_cv_.notify_all(); });
  SequenceAdmissionQueue::Ticket ticket(required);
  for (;;) {
    if (seq_stopping_) return -1;
    if (!gpu_healthy_.load(std::memory_order_relaxed)) return -3;
    if (detail::RequestCancelled(control, fd)) return -2;
    const int slot =
        seq_waiters_.Select(ticket, seq_capacities_, max_len_, seq_free_);
    // Refresh at handoff; waiting requests see a sample at least every 250 ms.
    // No slot is owned on rejection. The ticket destructor unlinks the waiter.
    const auto memory = memory_admission_.Observe(slot >= 0);
    if (!memory.Allowed())
      return memory.state == MemoryState::kLow ? -5 : -6;
    if (slot >= 0) {
      seq_free_[slot] = false;
      return slot;
    }
    if (!ticket.Queued()) {
      if (!seq_waiters_.Enqueue(ticket, admission_limits_.queued)) {
        metrics_.queue_rejected.fetch_add(1, std::memory_order_relaxed);
        return -4;
      }
      detail::ServeRequestFaultPoint("admission.queued", control->id().c_str());
    }
    // Socket EOF/deadlines need no extra watcher thread. Explicit cancellation
    // wakes this wait; timeout bounds polling even if a notification races it.
    seq_cv_.wait_for(lock, std::chrono::milliseconds(100));
  }
}

void ChatServer::FreeSeqId(int seq_id) {
  if (seq_id < 0) return;
  {
    const std::lock_guard<std::mutex> lock(seq_mu_);
    if (seq_id < max_seq_) seq_free_[static_cast<size_t>(seq_id)] = true;
  }
  seq_cv_.notify_all();
}

}  // namespace q4t::server
