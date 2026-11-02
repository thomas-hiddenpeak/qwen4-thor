// Runs the production AllocSeqId/FreeSeqId and RequestCancelled paths. Model
// ownership is unloaded by the lifecycle test seam; no CUDA/model/HTTP calls.
#include "contract_access.h"
#include "q4t/server/serve_fault.h"
#include "q4t/server/lifetime.h"
#include "q4t/test.h"

#include <sys/socket.h>
#include <unistd.h>

#include <chrono>
#include <future>
#include <stdexcept>

namespace {
using namespace std::chrono_literals;
using q4t::server::ChatServer;
using q4t::server::RequestControl;
using Access = q4t::server::ServeContractAccess;

// Cancellation/notification precede future destruction, including assertion
// and exception exits. A completed successful waiter retains only its own slot.
class Waiter {
 public:
  Waiter(ChatServer& server, std::string id, size_t required,
         std::chrono::milliseconds lifetime = 5s)
      : server_(server),
        control_(id, "key", std::chrono::steady_clock::now() + lifetime) {
    if (socketpair(AF_UNIX, SOCK_STREAM, 0, fd_))
      throw std::runtime_error("socketpair failed");
    try {
      future_ = std::async(std::launch::async, [this, required] {
        return Access::Allocate(server_, control_, fd_[0], required);
      });
    } catch (...) {
      close(fd_[0]);
      close(fd_[1]);
      throw;
    }
  }
  ~Waiter() {
    Cancel();
    try {
      if (future_.valid()) slot_ = future_.get();
    } catch (...) {
    }
    Release();
    close(fd_[0]);
    if (fd_[1] >= 0) close(fd_[1]);
  }
  void Cancel() {
    control_.Cancel();
    Access::Notify(server_);
  }
  void Disconnect() { close(std::exchange(fd_[1], -1)); }
  bool Ready(std::chrono::milliseconds timeout = 0ms) {
    return future_.wait_for(timeout) == std::future_status::ready;
  }
  int Get() {
    if (!Ready(2s)) throw std::runtime_error("admission did not complete");
    return slot_ = future_.get();
  }
  void Release() {
    if (slot_ >= 0) Access::Release(server_, std::exchange(slot_, -1));
  }

 private:
  ChatServer& server_;
  RequestControl control_;
  int fd_[2]{-1, -1};
  int slot_ = -1;
  std::future<int> future_;
};

bool WaitQueued(ChatServer& server, size_t count) {
  const auto end = std::chrono::steady_clock::now() + 2s;
  while (Access::Queued(server) != count) {
    if (std::chrono::steady_clock::now() >= end) return false;
    std::this_thread::sleep_for(1ms);
  }
  return true;
}
}  // namespace

Q4T_TEST(admission_runtime_fifo_survives_later_arrival_and_slot_transfer) {
  ChatServer server;
  Access::Slots(server, {8192}, 3);
  Waiter first(server, "first", 512);
  Q4T_CHECK(WaitQueued(server, 1));
  Waiter second(server, "second", 512);
  Q4T_CHECK(WaitQueued(server, 2));
  Waiter third(server, "third", 512);
  Q4T_CHECK(WaitQueued(server, 3));
  Access::Release(server, 0);
  Q4T_CHECK(first.Get() == 0);
  Q4T_CHECK(!second.Ready() && !third.Ready());
  first.Release();
  Q4T_CHECK(second.Get() == 0 && !third.Ready());
  second.Release();
  Q4T_CHECK(third.Get() == 0 && Access::Queued(server) == 0);
  third.Release();
  Q4T_CHECK(Access::Free(server) == 1 && Access::Rejected(server) == 0);
  return true;
}

Q4T_TEST(admission_runtime_zero_and_full_queue_reject_without_slot_loss) {
  for (int capacity : {0, 1}) {
    ChatServer server;
    Access::Slots(server, {8192}, capacity);
    std::unique_ptr<Waiter> first;
    if (capacity) {
      first = std::make_unique<Waiter>(server, "first", 512);
      Q4T_CHECK(WaitQueued(server, 1));
    }
    Waiter rejected(server, "rejected", 512);
    Q4T_CHECK(rejected.Get() == -4 && Access::Rejected(server) == 1);
    Q4T_CHECK(Access::Queued(server) == static_cast<size_t>(capacity));
    Q4T_CHECK(Access::Free(server) == 0);
    if (first) {
      first->Cancel();
      Q4T_CHECK(first->Get() == -2);
    }
    Access::Release(server, 0);
    Waiter recovery(server, "recovery", 512);
    Q4T_CHECK(recovery.Get() == 0 && Access::Queued(server) == 0);
  }
  return true;
}

Q4T_TEST(admission_runtime_full_queue_allows_incompatible_idle_slot) {
  ChatServer server;
  Access::Slots(server, {8192, 2048}, 1);
  Waiter large(server, "large", 4096);
  Q4T_CHECK(WaitQueued(server, 1));
  Access::Release(server, 1);
  Waiter small(server, "small", 1024);
  Q4T_CHECK(small.Get() == 1 && !large.Ready());
  Q4T_CHECK(Access::Queued(server) == 1 && Access::Rejected(server) == 0);
  Access::Release(server, 0);
  Q4T_CHECK(large.Get() == 0 && Access::Queued(server) == 0);
  return true;
}

Q4T_TEST(admission_runtime_cancel_head_middle_tail_wakes_successors) {
  for (int cancelled : {0, 1, 2}) {
    ChatServer server;
    Access::Slots(server, {8192}, 3);
    std::array<std::unique_ptr<Waiter>, 3> waiters;
    for (size_t i = 0; i < waiters.size(); ++i) {
      waiters[i] = std::make_unique<Waiter>(server, std::to_string(i), 512);
      Q4T_CHECK(WaitQueued(server, i + 1));
    }
    waiters[cancelled]->Cancel();
    Q4T_CHECK(waiters[cancelled]->Get() == -2 && Access::Queued(server) == 2);
    Access::Release(server, 0);
    for (int i = 0; i < 3; ++i) {
      if (i == cancelled) continue;
      Q4T_CHECK(waiters[i]->Get() == 0);
      waiters[i]->Release();
    }
    Q4T_CHECK(Access::Queued(server) == 0 && Access::Free(server) == 1);
  }
  return true;
}

Q4T_TEST(admission_runtime_deadline_disconnect_stop_and_gpu_failure_cleanup) {
  for (int reason = 0; reason < 4; ++reason) {
    ChatServer server;
    Access::Slots(server, {8192}, 1);
    Waiter waiter(server, "waiter", 512, reason == 0 ? 100ms : 5s);
    Q4T_CHECK(WaitQueued(server, 1));
    if (reason == 1) waiter.Disconnect();
    if (reason == 2) Access::StopAdmissions(server);
    if (reason == 3) {
      Access::Healthy(server, false);
      Access::Notify(server);
    }
    Q4T_CHECK(waiter.Get() == (reason <= 1 ? -2 : reason == 2 ? -1 : -3));
    Q4T_CHECK(Access::Queued(server) == 0 && Access::Free(server) == 0);
    Q4T_CHECK(Access::Rejected(server) == 0);
  }
  return true;
}

Q4T_TEST(admission_runtime_exception_unlinks_before_successor_recovery) {
  ChatServer server;
  Access::Slots(server, {8192}, 1);
  q4t::server::detail::serve_test_hook = +[](const char* name) {
    if (std::string_view(name) == "admission.queued") throw std::bad_alloc();
  };
  q4t::server::ScopeExit clear(
      [] { q4t::server::detail::serve_test_hook = nullptr; });
  {
    Waiter failed(server, "failed", 512);
    bool threw = false;
    try {
      failed.Get();
    } catch (const std::bad_alloc&) {
      threw = true;
    }
    Q4T_CHECK(threw && Access::Queued(server) == 0);
  }
  clear.Run();
  Waiter next(server, "next", 512);
  Q4T_CHECK(WaitQueued(server, 1));
  Access::Release(server, 0);
  Q4T_CHECK(next.Get() == 0 && Access::Queued(server) == 0);
  return true;
}
