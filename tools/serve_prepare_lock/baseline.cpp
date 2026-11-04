// Baseline probe: production blocking diagnostics lock ignores cancellation.
#include <chrono>
#include <cstdio>
#include <future>
#include <mutex>
#include <thread>

#include "q4t/server/request_control.h"
#include "q4t/server/request_diagnostics.h"

int main() {
  using namespace std::chrono_literals;
  using Clock = std::chrono::steady_clock;
  using q4t::server::RequestControl;
  using q4t::server::RequestDiagnostics;
  for (const bool deadline : {false, true}) {
    std::mutex mutex;
    std::unique_lock owner(mutex);
    RequestControl request(
        "baseline", "key",
        deadline ? Clock::now() + 40ms : Clock::time_point::max());
    std::promise<void> entered;
    auto ready = entered.get_future();
    const auto began = Clock::now();
    auto waiter = std::async(std::launch::async, [&] {
      RequestDiagnostics diagnostic;
      entered.set_value();
      auto lock = diagnostic.Lock(mutex, RequestDiagnostics::Wait::kTokenizer);
      return lock.owns_lock() && request.Cancelled();
    });
    ready.wait();
    std::this_thread::sleep_for(60ms);
    if (!deadline) request.Cancel();
    const bool cancelled = request.Cancelled();
    const bool returned = waiter.wait_for(200ms) == std::future_status::ready;
    owner.unlock();
    const bool acquired_after_cancel = waiter.get();
    const auto elapsed = std::chrono::duration<double>(Clock::now() - began);
    std::printf(
        "{\"case\":\"%s\",\"cancelled\":%s,"
        "\"returned_while_held\":%s,\"acquired_after_cancel\":%s,"
        "\"seconds\":%.6f}\n",
        deadline ? "deadline" : "explicit_cancel", cancelled ? "true" : "false",
        returned ? "true" : "false", acquired_after_cancel ? "true" : "false",
        elapsed.count());
    if (!cancelled || returned || !acquired_after_cancel) return 1;
  }
}
