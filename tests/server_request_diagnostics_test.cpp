#include "q4t/server/request_diagnostics.h"
#include "q4t/test.h"

#include <future>
#include <stdexcept>

namespace {
std::string Read(FILE* file) {
  std::rewind(file);
  std::string output;
  char buffer[1024];
  while (std::fgets(buffer, sizeof(buffer), file)) output += buffer;
  return output;
}
}  // namespace

Q4T_TEST(request_diagnostics_terminal_and_reuse) {
  FILE* file = std::tmpfile();
  Q4T_CHECK(file);
  for (int i = 0; i < 2; ++i) {
    q4t::server::RequestDiagnostics d(file, "trace-test");
    d.Bind("reused-id");
    d.Admitted(0);
    d.Stage("prefill");
    d.Released();
    d.Outcome(i ? "cancelled" : "success",
              i ? "explicit_cancel" : "completed");
  }
  const auto text = Read(file);
  std::fclose(file);
  Q4T_CHECK(text.find("released=1 outcome=success") != std::string::npos);
  Q4T_CHECK(text.find("released=1 outcome=cancelled") != std::string::npos);
  Q4T_CHECK(text.find("trace_id=trace-test") != std::string::npos);
  const auto first = text.find("instance=");
  const auto next = text.find("event=begin", text.find("event=begin") + 1);
  Q4T_CHECK(next != std::string::npos && first != std::string::npos);
  const auto second = text.rfind("instance=", next);
  Q4T_CHECK(text.substr(first, text.find(' ', first) - first) !=
            text.substr(second, text.find(' ', second) - second));
  return true;
}

Q4T_TEST(request_diagnostics_wait_accounting) {
  using D = q4t::server::RequestDiagnostics;
  FILE* file = std::tmpfile();
  Q4T_CHECK(file);
  {
    D d(file);
    d.Observe(D::Wait::kSlot, std::chrono::milliseconds(7));
    d.Observe(D::Wait::kScheduler, std::chrono::milliseconds(11));
    d.Observe(D::Wait::kScheduler, std::chrono::milliseconds(13));
    Q4T_CHECK(d.Total(D::Wait::kSlot) == 7);
    Q4T_CHECK(d.Total(D::Wait::kScheduler) == 24);
    std::mutex mutex;
    { auto lock = d.Lock(mutex, D::Wait::kModel); }
    Q4T_CHECK(mutex.try_lock());
    mutex.unlock();
  }
  const auto text = Read(file);
  std::fclose(file);
  Q4T_CHECK(text.find("scheduler_waits=2") != std::string::npos);
  Q4T_CHECK(text.find("outcome=error") != std::string::npos);
  Q4T_CHECK(text.find("id=unregistered") != std::string::npos);
  return true;
}

Q4T_TEST(request_lock_ready_and_already_cancelled) {
  std::mutex mutex;
  {
    auto lock = q4t::server::LockUnlessCancelled(mutex, [] { return false; });
    Q4T_CHECK(lock.owns_lock());
  }
  auto lock = q4t::server::LockUnlessCancelled(mutex, [] { return true; });
  Q4T_CHECK(!lock.owns_lock());
  Q4T_CHECK(mutex.try_lock());
  mutex.unlock();
  return true;
}

Q4T_TEST(request_lock_cancelled_waiter_retires_before_owner) {
  using namespace std::chrono_literals;
  using q4t::server::RequestControl;
  for (const auto reason : {RequestControl::State::kExplicit,
                            RequestControl::State::kDisconnect,
                            RequestControl::State::kShutdown,
                            RequestControl::State::kFailure}) {
    std::mutex mutex;
    std::unique_lock owner(mutex);
    RequestControl request("waiter", "key",
                           std::chrono::steady_clock::time_point::max());
    std::promise<void> checked;
    auto entered = checked.get_future();
    auto waiter = std::async(std::launch::async, [&] {
      bool first = true;
      auto lock = q4t::server::LockUnlessCancelled(mutex, [&] {
        if (first) {
          first = false;
          checked.set_value();
        }
        return request.Cancelled();
      });
      return !lock.owns_lock();
    });
    entered.wait();
    request.Cancel(reason);
    const bool before_release =
        waiter.wait_for(1s) == std::future_status::ready;
    owner.unlock();  // Always release before a failed assertion/async teardown.
    const bool rejected = waiter.get();
    Q4T_CHECK(before_release && rejected && request.Cancelled());
  }
  return true;
}

Q4T_TEST(request_lock_deadline_and_wait_diagnostics) {
  using namespace std::chrono_literals;
  using Clock = std::chrono::steady_clock;
  using q4t::server::RequestControl;
  using q4t::server::RequestDiagnostics;
  std::mutex mutex;
  std::unique_lock owner(mutex);
  FILE* file = std::tmpfile();
  Q4T_CHECK(file);
  auto waiter = std::async(std::launch::async, [&] {
    RequestControl request("deadline", "key", Clock::now() + 20ms);
    RequestDiagnostics diagnostic(file);
    auto lock = diagnostic.Lock(mutex, RequestDiagnostics::Wait::kTokenizer,
                                [&] { return request.Cancelled(); });
    return !lock.owns_lock() &&
           std::string(request.Reason()) == "deadline_exceeded" &&
           diagnostic.Total(RequestDiagnostics::Wait::kTokenizer) > 0;
  });
  const bool before_release = waiter.wait_for(1s) == std::future_status::ready;
  owner.unlock();
  const bool rejected = waiter.get();
  const auto text = Read(file);
  std::fclose(file);
  Q4T_CHECK(before_release && rejected);
  Q4T_CHECK(text.find("tokenizer_lock_ms=") != std::string::npos);
  return true;
}

Q4T_TEST(request_lock_handoff_cancellation_releases_lock) {
  std::mutex mutex;
  int checks = 0;
  auto lock = q4t::server::LockUnlessCancelled(
      mutex, [&] { return ++checks > 1; });
  Q4T_CHECK(!lock.owns_lock());
  Q4T_CHECK(mutex.try_lock());
  mutex.unlock();
  return true;
}

Q4T_TEST(request_lock_predicate_exception_releases_lock) {
  for (const int fail_at : {1, 2}) {
    std::mutex mutex;
    int checks = 0;
    bool caught = false;
    try {
      auto lock = q4t::server::LockUnlessCancelled(mutex, [&] {
        if (++checks == fail_at) throw std::runtime_error("cancel failed");
        return false;
      });
    } catch (const std::runtime_error&) {
      caught = true;
    }
    Q4T_CHECK(caught && mutex.try_lock());
    mutex.unlock();
  }
  return true;
}
