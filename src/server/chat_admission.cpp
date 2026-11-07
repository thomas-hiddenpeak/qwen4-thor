// Slot ownership and bounded waiting, independent of model execution.
#include "chat_server_internal.h"

namespace q4t::server {

int ChatServer::AllocSeqId(RequestControl* control, int fd, size_t required) {
  std::unique_lock<std::mutex> lock(seq_mu_);
  // Destruction order: unlink ticket, notify successors, then unlock. Every
  // return/exception removes exactly this waiter without allocating storage.
  ScopeExit notify([&] { seq_cv_.notify_all(); });
  const auto* http = detail::HttpResponseContext::Current(fd);
  SequenceAdmissionQueue::Ticket ticket(
      required, http ? http->Client().Index() : kMaxClients);
  for (;;) {
    if (seq_stopping_) {
      // Use the cancellation path for both the response and abort accounting.
      // Cancel preserves an earlier terminal reason if it already won.
      control->Cancel(RequestControl::State::kShutdown);
      return -2;
    }
    if (!ObserveHealth().Healthy()) return -3;
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
      seq_waiters_.Granted(ticket);
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
