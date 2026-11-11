#include "q4t/server/application_owner.h"

#include <atomic>
#include <thread>

#include "q4t/io/json.h"
#include "q4t/test.h"

namespace {
using namespace q4t::server;
using namespace std::chrono_literals;
using Clock = std::chrono::steady_clock;
bool Wait(const auto& predicate) {
  const auto until = Clock::now() + 2s;
  while (Clock::now() < until) {
    if (predicate()) return true;
    std::this_thread::sleep_for(1ms);
  }
  return predicate();
}
struct Owner {
  std::atomic<bool> stopped{false};
  int value = 42;
  void RequestStop() noexcept { stopped = true; }
  void Join() {}
};
}  // namespace

Q4T_TEST(application_startup_retry_window_bounds_attempts_and_health) {
  ApplicationStartup startup({2ms, 200ms});
  std::atomic<int> calls{0};
  const auto fail = [&] {
    ++calls;
    throw std::bad_alloc();
  };
  Q4T_CHECK(!startup.Try(fail) && calls == 1);
  Q4T_CHECK(Wait([&] {
    startup.Try(fail);
    return calls == 3;
  }));
  const auto h = startup.Health();
  std::printf("[application-startup-health] %s\n",
              ApplicationStartupJson(h).c_str());
  Q4T_CHECK(h.attempts == 3 && h.failures == 3 && !h.ready &&
            h.retry_after_ms > 100 && h.retry_after_ms <= 200);
  for (size_t i = 0; i < 100; ++i) Q4T_CHECK(!startup.Try(fail));
  Q4T_CHECK(calls == 3);
  Q4T_CHECK(Wait([&] { return startup.Try([&] { ++calls; }); }));
  const auto ready = startup.Health();
  std::printf("[application-startup-health] %s\n",
              ApplicationStartupJson(ready).c_str());
  Q4T_CHECK(calls == 4 && ready.ready && ready.recoveries == 1 &&
            ready.failures == 3 && ready.retry_after_ms == 0);
  Q4T_CHECK(!startup.Try(fail) && calls == 4);
  q4t::io::Json health;
  Q4T_CHECK(q4t::io::ParseJson(ApplicationStartupJson(ready), &health) &&
            health.GetString("state") == "ready" &&
            health.GetString("last_failure") == "host_memory" &&
            !health.GetBool("readiness_dependency", true));
  return true;
}

Q4T_TEST(application_owner_concurrent_initialization_publishes_once) {
  ApplicationOwner<Owner> owner;
  std::atomic<bool> entered{false}, release{false};
  std::atomic<int> calls{0};
  const auto factory = [&] {
    ++calls;
    entered = true;
    while (!release) std::this_thread::sleep_for(1ms);
    return std::make_unique<Owner>();
  };
  std::thread create([&] { owner.TryStart(factory); });
  ScopeExit cleanup([&] {
    release = true;
    create.join();
  });
  Q4T_CHECK(Wait([&] { return entered.load(); }));
  for (size_t i = 0; i < 100; ++i) {
    Q4T_CHECK(owner.Get() == nullptr && owner.StartupHealth().starting &&
              !owner.TryStart(factory));
  }
  release = true;
  Q4T_CHECK(Wait([&] { return owner.Get() != nullptr; }));
  auto* published = owner.Get();
  Q4T_CHECK(published->value == 42 && calls == 1);
  for (size_t i = 0; i < 100; ++i)
    Q4T_CHECK(!owner.TryStart(factory) && owner.Get() == published);
  cleanup.Run();
  owner.RequestStop();
  owner.Join();
  Q4T_CHECK(owner->stopped && owner.StartupHealth().attempts == 1);
  return true;
}

Q4T_TEST(
    application_owner_stop_during_creation_preserves_single_stopped_owner) {
  ApplicationOwner<Owner> owner;
  std::atomic<bool> entered{false}, release{false};
  std::atomic<int> calls{0};
  const auto factory = [&] {
    ++calls;
    entered = true;
    while (!release) std::this_thread::sleep_for(1ms);
    return std::make_unique<Owner>();
  };
  std::thread create([&] { owner.TryStart(factory); });
  ScopeExit cleanup([&] {
    release = true;
    create.join();
  });
  Q4T_CHECK(Wait([&] { return entered.load(); }));
  owner.RequestStop();
  Q4T_CHECK(!owner && owner.StartupHealth().stopping);
  release = true;
  cleanup.Run();
  Q4T_CHECK(owner && owner->stopped && calls == 1 && !owner.TryStart(factory));
  owner.Join();
  return true;
}

Q4T_TEST(application_owner_stop_before_creation_prevents_all_attempts) {
  ApplicationOwner<Owner> owner;
  ApplicationStartup scanner;
  owner.PreventStart();
  scanner.PreventStart();
  int calls = 0;
  Q4T_CHECK(!owner.TryStart([&] {
    ++calls;
    return std::make_unique<Owner>();
  }) && !scanner.Try([&] { ++calls; }) &&
            !owner && calls == 0 && owner.StartupHealth().attempts == 0 &&
            scanner.Health().attempts == 0);
  owner.RequestStop();
  owner.Join();
  return true;
}

Q4T_TEST(application_startup_classifies_failure_and_domains_are_independent) {
  ApplicationStartup response, scanner;
  ApplicationOwner<Owner> batch;
  Q4T_CHECK(!response.Try([] { throw std::bad_alloc(); }));
  Q4T_CHECK(!scanner.Try([] {
    throw std::system_error(std::make_error_code(std::errc::no_such_process));
  }));
  Q4T_CHECK(batch.TryStart([] { return std::make_unique<Owner>(); }) &&
            std::string(response.Health().last_failure) == "host_memory" &&
            std::string(scanner.Health().last_failure) == "thread_or_system" &&
            batch.StartupHealth().ready && batch->value == 42);
  ApplicationOwner<Owner> missing({1ms, 10ms});
  Q4T_CHECK(!missing.TryStart([] { return std::unique_ptr<Owner>(); }) &&
            !missing && missing.StartupHealth().failures == 1 &&
            std::string(missing.StartupHealth().last_failure) ==
                "initialization_exception");
  Q4T_CHECK(Wait([&] {
    return missing.TryStart([] { return std::make_unique<Owner>(); });
  }));
  // A published owner which is stopping cannot be replaced by a new owner.
  missing->RequestStop();
  Q4T_CHECK(!missing.TryStart([] { return std::make_unique<Owner>(); }) &&
            missing->stopped && missing.StartupHealth().recoveries == 1);
  return true;
}
