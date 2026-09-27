#include "q4t/server/request_control.h"

#include <cassert>
#include <thread>

using q4t::server::RequestControl;
using q4t::server::RequestRegistry;
using Clock = std::chrono::steady_clock;
int main() {
  const auto never = Clock::time_point::max();
  for (int i = 0; i < 2000; ++i) {
    RequestControl r("race", "key", never);
    std::atomic<bool> go{false};
    bool cancel_won = false;
    std::thread cancel([&] {
      while (!go.load()) std::this_thread::yield();
      cancel_won = r.Cancel();
    });
    go.store(true);
    const bool completed = r.Finish();
    cancel.join();
    assert(cancel_won != completed);
    assert(r.Cancelled() == cancel_won);
    assert(r.Cancel() == cancel_won);
    assert(r.Finish() == completed);
  }
  RequestRegistry registry;
  auto old = registry.Register("reuse", "old-key", never);
  assert(old && !registry.Register("reuse", "new-key", never));
  assert(!registry.Cancel("reuse", "wrong"));
  assert(registry.Cancel("reuse", "old-key"));
  assert(registry.Cancel("reuse", "old-key"));
  registry.Remove(old);
  assert(!registry.Cancel("reuse", "old-key"));
  auto current = registry.Register("reuse", "new-key", never);
  assert(current);
  registry.Remove(old);  // Late cleanup must not erase the new generation.
  assert(!registry.Cancel("reuse", "old-key"));
  assert(!current->Cancelled());
  assert(registry.Cancel("reuse", "new-key"));
  registry.Remove(current);
  RequestControl expired("deadline", "key", Clock::now());
  assert(expired.Cancelled());
  assert(std::string(expired.Reason()) == "deadline_exceeded");
  assert(!expired.Finish());
  auto shutdown = registry.Register("shutdown", "key", never);
  registry.CancelAll();
  assert(shutdown->Cancelled());
  assert(std::string(shutdown->Reason()) == "server_shutdown");
  registry.Remove(shutdown);
}
