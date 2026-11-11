// Publish-once auxiliary ownership. Readers never wait for initialization.
#pragma once

#include <array>
#include <atomic>
#include <chrono>
#include <cstdint>
#include <memory>
#include <new>
#include <stdexcept>
#include <string>
#include <system_error>
#include <utility>

#include "q4t/server/lifetime.h"

namespace q4t::server {
enum class ApplicationThreadKind {
  kResponseWorker,
  kResponseScanner,
  kBatchWorker,
  kBatchScanner
};
#ifdef Q4T_GRAMMAR_PROCESS_TESTS
inline std::atomic<int> application_failed_thread{-1};
inline std::atomic<size_t> application_threads_before_failure{0};
inline void FailApplicationThreadForTesting(ApplicationThreadKind kind,
                                            size_t after = 0) {
  application_threads_before_failure.store(after);
  application_failed_thread.store(static_cast<int>(kind));
}
#endif
inline void ApplicationThreadStart(ApplicationThreadKind kind) {
#ifdef Q4T_GRAMMAR_PROCESS_TESTS
  if (application_failed_thread.load() == static_cast<int>(kind) &&
      application_threads_before_failure.fetch_sub(1) == 0) {
    application_failed_thread.store(-1);
    throw std::system_error(
        std::make_error_code(std::errc::resource_unavailable_try_again));
  }
#else
  (void)kind;
#endif
}
struct ApplicationStartupOptions {
  std::chrono::milliseconds delay{250};
  std::chrono::milliseconds window{60000};
};
struct ApplicationStartupHealth {
  bool ready = false, starting = false, stopping = false;
  uint64_t attempts = 0, failures = 0, recoveries = 0;
  int64_t retry_after_ms = 0;
  const char* last_failure = "none";
};
std::string ApplicationStartupJson(const ApplicationStartupHealth& health);

// At most three failed attempts in a rolling window. Success is permanent;
// retry never replaces an owner which may still hold execution resources.
class ApplicationStartup {
 public:
  explicit ApplicationStartup(ApplicationStartupOptions options = {})
      : options_(options) {
    if (options.delay.count() < 1 || options.window < options.delay ||
        options.window > std::chrono::hours(24))
      throw std::invalid_argument("Invalid application startup retry limits");
  }
  template <typename Function>
  bool Try(Function&& initialize) noexcept {
    if (ready_.load() || stopping_.load() || Now() < retry_at_.load() ||
        starting_.test_and_set())
      return false;
    ScopeExit unlock([&] { starting_.clear(); });
    if (ready_.load() || stopping_.load() || Now() < retry_at_.load())
      return false;
    attempts_.fetch_add(1);
    try {
      initialize();
      if (failures_.load()) recoveries_.fetch_add(1);
      retry_at_.store(0);
      ready_.store(true);
      return true;
    } catch (const std::bad_alloc&) {
      last_failure_.store("host_memory");
    } catch (const std::system_error&) {
      last_failure_.store("thread_or_system");
    } catch (...) {
      last_failure_.store("initialization_exception");
    }
    Failure();
    return false;
  }
  // Async-signal-safe: ChatServer's signal path uses only lock-free bools.
  void PreventStart() noexcept {
    static_assert(std::atomic<bool>::is_always_lock_free);
    stopping_.store(true);
  }
  bool Stopping() const noexcept { return stopping_.load(); }
  ApplicationStartupHealth Health() const noexcept;

 private:
  static int64_t Now() noexcept;
  void Failure() noexcept;
  const ApplicationStartupOptions options_;
  std::atomic_flag starting_ = ATOMIC_FLAG_INIT;
  std::atomic<bool> ready_{false}, stopping_{false};
  std::atomic<uint64_t> attempts_{0}, failures_{0}, recoveries_{0};
  std::atomic<int64_t> retry_at_{0};
  std::atomic<const char*> last_failure_{"none"};
  // Only the single successful starting_ claimant accesses these fields.
  std::array<int64_t, 3> recent_{};
  size_t count_ = 0;
};

template <typename Owner>
class ApplicationOwner {
 public:
  explicit ApplicationOwner(ApplicationStartupOptions options = {})
      : startup_(options) {}
  template <typename Factory>
  bool TryStart(Factory&& factory) noexcept {
    return startup_.Try([&] {
      auto owner = factory();
      if (!owner) throw std::runtime_error("Missing application owner");
      if (startup_.Stopping()) owner->RequestStop();
      owned_ = std::move(owner);
      published_.store(owned_.get(), std::memory_order_release);
      // Close a stop arriving between the earlier check and publication.
      if (startup_.Stopping()) owned_->RequestStop();
    });
  }
  Owner* Get() const noexcept {
    return published_.load(std::memory_order_acquire);
  }
  explicit operator bool() const noexcept { return Get() != nullptr; }
  Owner* operator->() const noexcept { return Get(); }
  Owner& operator*() const noexcept { return *Get(); }
  void PreventStart() noexcept { startup_.PreventStart(); }
  void RequestStop() noexcept {
    PreventStart();
    if (auto* owner = Get()) owner->RequestStop();
  }
  // The creation coordinator calls Join after its last TryStart. Readers and
  // task owners must drain before destruction; the address never changes.
  void Join() {
    if (auto* owner = Get()) owner->Join();
  }
  ApplicationStartupHealth StartupHealth() const noexcept {
    return startup_.Health();
  }

 private:
  ApplicationStartup startup_;
  std::unique_ptr<Owner> owned_;
  std::atomic<Owner*> published_{nullptr};
};
}  // namespace q4t::server
