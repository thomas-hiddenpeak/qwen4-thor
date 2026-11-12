#include "q4t/server/session_cache.h"

#include <signal.h>
#include <sys/stat.h>
#include <unistd.h>

#include <chrono>
#include <cstdlib>
#include <cstring>
#include <filesystem>
#include <thread>

#include "q4t/io/json.h"
#include "q4t/server/cache_health.h"
#include "q4t/test.h"

namespace q4t::server {
struct SessionCacheTestPeer {
  static void Age(SessionCache& cache, const std::string& key,
                  std::chrono::seconds elapsed) {
    const std::lock_guard lock(cache.mutex_);
    auto& entry = *cache.entries_.at(key);
    entry.used -= elapsed;
    entry.expires -= elapsed;
    if (entry.lifetime.max_age_seconds) entry.retention_end -= elapsed;
  }
  static std::chrono::steady_clock::time_point Expiry(SessionCache& cache,
                                                     const std::string& key) {
    const std::lock_guard lock(cache.mutex_);
    return cache.entries_.at(key)->expires;
  }
  static bool AtRetentionEnd(SessionCache& cache, const std::string& key) {
    const std::lock_guard lock(cache.mutex_);
    const auto& entry = *cache.entries_.at(key);
    return entry.expires == entry.retention_end;
  }
  static bool Recover(SessionCache& cache) {
    const auto end = std::chrono::steady_clock::now() + std::chrono::seconds(2);
    while (std::chrono::steady_clock::now() < end) {
      if (cache.io_->Initialize()) return true;
      std::this_thread::sleep_for(std::chrono::milliseconds(10));
    }
    return false;
  }
};
}  // namespace q4t::server

namespace {
using namespace q4t::server;
using namespace std::chrono_literals;
using Clock = std::chrono::steady_clock;
namespace fs = std::filesystem;
struct Fixture {
  std::string path;
  std::vector<int32_t> prefix = std::vector<int32_t>(8193, 78);
  std::vector<uint8_t> data =
      std::vector<uint8_t>(SessionCache::kStagingBytes + 71, 91);
  std::vector<SessionRange> Ranges() { return {{data.data(), data.size()}}; }
  Fixture() {
    const auto* output = std::getenv("Q4T_TEST_OUTPUT_DIR");
    auto root =
        fs::absolute(output ? output : ".q4t-work/session-isolation-host");
    fs::create_directories(root);
    path = (root / "session-XXXXXX").string();
    if (!mkdtemp(path.data())) throw std::runtime_error("session directory");
  }
};
const auto Copy = [](void* host, void* state, size_t bytes, bool restore) {
  std::memcpy(restore ? state : host, restore ? host : state, bytes);
  return q4t::Status();
};
const auto Active = [] { return false; };
}  // namespace

Q4T_TEST(session_cache_long_ttl_survives_short_read_without_restarting_long_ttl) {
  Fixture f;
  f.data.resize(32);
  SessionCache cache;
  Q4T_CHECK(cache.Open(f.path));
  Q4T_CHECK(cache.AcquireNew("scope:key"));
  Q4T_CHECK(cache.Save("scope:key", f.prefix, f.Ranges(), Copy, Active, 71,
                       nullptr, SessionLayout::kMtp, {43200, 1800, 86400}));
  cache.Release("scope:key", true);
  SessionCacheTestPeer::Age(cache, "scope:key", 31min);
  const auto expiry = SessionCacheTestPeer::Expiry(cache, "scope:key");
  std::string selected;
  Q4T_CHECK(cache.MatchShared("scope", f.prefix, &selected) == 8192);
  int32_t d0 = -1;
  Q4T_CHECK(cache.Restore(selected, f.Ranges(), Copy, Active, &d0));
  Q4T_CHECK(d0 == 71 && SessionCacheTestPeer::Expiry(cache, selected) == expiry);
  cache.Release(selected, true);
  SessionCacheTestPeer::Age(cache, selected, 12h);
  Q4T_CHECK(cache.MatchShared("scope", f.prefix, &selected) == 0);
  return true;
}

Q4T_TEST(session_cache_read_refresh_cannot_extend_total_retention) {
  Fixture f;
  f.data.resize(32);
  SessionCache cache;
  Q4T_CHECK(cache.Open(f.path));
  Q4T_CHECK(cache.Acquire("same"));
  Q4T_CHECK(cache.Save("same", f.prefix, f.Ranges(), Copy, Active, 71, nullptr,
                       SessionLayout::kMtp, {1800, 1800, 3600}));
  cache.Release("same", true);
  for (int i = 0; i < 2; ++i) {
    SessionCacheTestPeer::Age(cache, "same", 25min);
    Q4T_CHECK(cache.Acquire("same") && cache.Match("same", f.prefix) == 8192);
    Q4T_CHECK(cache.Restore("same", f.Ranges(), Copy, Active, nullptr));
    cache.Release("same", true);
  }
  Q4T_CHECK(SessionCacheTestPeer::AtRetentionEnd(cache, "same"));
  SessionCacheTestPeer::Age(cache, "same", 11min);
  Q4T_CHECK(cache.Acquire("same") && cache.Match("same", f.prefix) == 0);
  cache.Release("same", false);
  return true;
}

Q4T_TEST(session_cache_lookup_only_reuses_requested_boundaries) {
  Fixture f;
  f.data.resize(32);
  SessionCache cache;
  Q4T_CHECK(cache.Open(f.path) && cache.AcquireNew("scope:key"));
  Q4T_CHECK(cache.Save("scope:key", f.prefix, f.Ranges(), Copy, Active, 71));
  cache.Release("scope:key", true);
  std::string key;
  const std::vector<int> wrong = {4096}, exact = {8192};
  Q4T_CHECK(cache.MatchShared("scope", f.prefix, &key, SessionLayout::kMtp,
                              wrong) == 0 && key.empty());
  Q4T_CHECK(cache.MatchShared("scope", f.prefix, &key, SessionLayout::kMtp,
                              exact) == 8192 && key == "scope:key");
  cache.Release(key, true);
  return true;
}

Q4T_TEST(session_cache_lifetime_upgrade_requires_a_real_committed_replacement) {
  Fixture f;
  f.data.resize(32);
  SessionCache cache;
  Q4T_CHECK(cache.Open(f.path) && cache.AcquireNew("scope:key"));
  Q4T_CHECK(cache.Save("scope:key", f.prefix, f.Ranges(), Copy, Active, 71));
  cache.Release("scope:key", true);
  Q4T_CHECK(!cache.AcquireNew("scope:key", 1800));
  Q4T_CHECK(cache.AcquireNew("scope:key", 43200));
  Q4T_CHECK(!cache.Save("scope:key", f.prefix, f.Ranges(), Copy, Active, 71,
                        nullptr, SessionLayout::kMtp, {43200, 1800, 3600}));
  cache.Release("scope:key", false);
  Q4T_CHECK(cache.AcquireNew("scope:key", 43200));
  Q4T_CHECK(cache.Save("scope:key", f.prefix, f.Ranges(), Copy, Active, 71,
                       nullptr, SessionLayout::kMtp, {43200, 1800, 86400}));
  cache.Release("scope:key", true);
  SessionCacheTestPeer::Age(cache, "scope:key", 31min);
  std::string selected;
  Q4T_CHECK(cache.MatchShared("scope", f.prefix, &selected) == 8192);
  cache.Release(selected, true);
  return true;
}

Q4T_TEST(session_cache_unavailable_directory_preserves_session_contract) {
  Fixture f;
  f.path += "/later";
  SessionCache cache;
  Q4T_CHECK(!cache.Open(f.path) && cache.Enabled());
  Q4T_CHECK(cache.Health().configured && !cache.Health().available);
  Q4T_CHECK(cache.Acquire("same") && !cache.Acquire("same"));
  Q4T_CHECK(cache.Match("same", f.prefix) == 0);
  const auto start = Clock::now();
  Q4T_CHECK(!cache.Save("same", f.prefix, f.Ranges(), Copy, Active, 71));
  Q4T_CHECK(Clock::now() - start <
            200ms);  // Backoff, no repeated filesystem wait.
  cache.Release("same", false);
  Q4T_CHECK(cache.Acquire("same"));
  fs::create_directory(f.path);
  chmod(f.path.c_str(), 0700);
  Q4T_CHECK(SessionCacheTestPeer::Recover(cache));
  Q4T_CHECK(cache.Save("same", f.prefix, f.Ranges(), Copy, Active, 71));
  Q4T_CHECK(cache.Match("same", f.prefix) == 0);
  cache.Release("same", true);
  Q4T_CHECK(cache.Acquire("same") && cache.Match("same", f.prefix) == 8192);
  return true;
}

Q4T_TEST(session_cache_partial_restore_loss_evicts_generation_and_rebuilds) {
  Fixture f;
  SessionCache cache;
  SessionCache prefix(IsolatedWorkerDomain::kPrefixCache);
  Q4T_CHECK(cache.Open(f.path) && prefix.Open(f.path));
  const auto prefix_pid = prefix.Health().worker_pid;
  Q4T_CHECK(cache.Acquire("same"));
  Q4T_CHECK(cache.Save("same", f.prefix, f.Ranges(), Copy, Active, 71));
  cache.Release("same", true);
  Q4T_CHECK(cache.Acquire("same"));
  const auto old_generation = cache.Health().generation;
  std::fill(f.data.begin(), f.data.end(), 0);
  int copies = 0;
  const auto copy_and_stop = [&](void* host, void* state, size_t n,
                                 bool restore) {
    Copy(host, state, n, restore);
    if (++copies == 1 && kill(cache.Health().worker_pid, SIGSTOP))
      return q4t::Status::Fail("owned worker pause");
    return q4t::Status();
  };
  const auto start = Clock::now();
  int32_t d0 = -1;
  Q4T_CHECK(!cache.Restore("same", f.Ranges(), copy_and_stop, Active, &d0));
  Q4T_CHECK(Clock::now() - start < 2300ms && copies == 1 && d0 == -1);
  Q4T_CHECK(f.data.front() == 91 &&
            f.data.back() == 0);  // Actual partial copy.
  Q4T_CHECK(cache.Match("same", f.prefix) == 0 && cache.Bytes() == 0);
  Q4T_CHECK(
      !cache.Acquire("same"));  // Loss did not release another request's lease.
  Q4T_CHECK(prefix.Health().available &&
            prefix.Health().worker_pid == prefix_pid);
  // The caller must reset partial model state; this host oracle supplies a new
  // complete snapshot and verifies it survives only after a successful release.
  std::fill(f.data.begin(), f.data.end(), 37);
  Q4T_CHECK(SessionCacheTestPeer::Recover(cache));
  Q4T_CHECK(cache.Health().generation != old_generation);
  Q4T_CHECK(cache.Save("same", f.prefix, f.Ranges(), Copy, Active, 93));
  cache.Release("same", true);
  Q4T_CHECK(cache.Acquire("same"));
  std::fill(f.data.begin(), f.data.end(), 0);
  Q4T_CHECK(cache.Restore("same", f.Ranges(), Copy, Active, &d0));
  Q4T_CHECK(d0 == 93 && f.data == std::vector<uint8_t>(
                                      SessionCache::kStagingBytes + 71, 37));
  return true;
}

Q4T_TEST(session_cache_cancelled_write_is_unpublished_and_not_replayed) {
  Fixture f;
  SessionCache cache;
  Q4T_CHECK(cache.Open(f.path) && cache.Acquire("same"));
  const auto old = cache.Health();
  // Pre-dispatch cancellation must not kill an untouched healthy cache.
  Q4T_CHECK(
      !cache.Save("same", f.prefix, f.Ranges(), Copy, [] { return true; }, 4));
  Q4T_CHECK(cache.Health().available &&
            cache.Health().worker_pid == old.worker_pid);
  int copies = 0;
  Clock::time_point paused;
  const auto copy_and_stop = [&](void* host, void* state, size_t n,
                                 bool restore) {
    Copy(host, state, n, restore);
    ++copies;
    paused = Clock::now();
    return kill(cache.Health().worker_pid, SIGSTOP) == 0
               ? q4t::Status()
               : q4t::Status::Fail("owned worker pause");
  };
  const auto cancelled = [&] { return copies && Clock::now() - paused > 30ms; };
  Q4T_CHECK(
      !cache.Save("same", f.prefix, f.Ranges(), copy_and_stop, cancelled, 4));
  Q4T_CHECK(Clock::now() - paused < 200ms && copies == 1);
  cache.Release("same", true);  // Even an erroneous success cannot publish it.
  Q4T_CHECK(cache.Acquire("same") && !cache.Match("same", f.prefix));
  Q4T_CHECK(SessionCacheTestPeer::Recover(cache));
  Q4T_CHECK(cache.Health().workers_started == 2 && copies == 1);
  Q4T_CHECK(!cache.Match("same", f.prefix) && cache.Bytes() == 0);
  return true;
}

Q4T_TEST(session_cache_cancellation_before_write_never_waits_for_cleanup) {
  Fixture f;
  f.data.resize(4096);
  SessionCache cache;
  Q4T_CHECK(cache.Open(f.path) && cache.Acquire("same"));
  bool cancelled = false;
  int copies = 0;
  const auto copy_and_cancel = [&](void* host, void* state, size_t n,
                                   bool restore) {
    Copy(host, state, n, restore);
    ++copies;
    cancelled = true;
    return kill(cache.Health().worker_pid, SIGSTOP) == 0
               ? q4t::Status()
               : q4t::Status::Fail("owned worker pause");
  };
  const auto start = Clock::now();
  const auto status = cache.Save("same", f.prefix, f.Ranges(), copy_and_cancel,
                                  [&] { return cancelled; }, 4);
  const auto elapsed = Clock::now() - start;
  std::printf("[CACHE_CANCEL_BEFORE_WRITE] seconds=%.6f status=%s\n",
              std::chrono::duration<double>(elapsed).count(),
              status.message().c_str());
  Q4T_CHECK(!status && status.message() == "cache storage cancelled");
  Q4T_CHECK(elapsed < 200ms && copies == 1);
  cache.Release("same", true);
  Q4T_CHECK(cache.Acquire("same") && !cache.Match("same", f.prefix));
  Q4T_CHECK(SessionCacheTestPeer::Recover(cache));
  Q4T_CHECK(cache.Health().workers_started == 2 && copies == 1);
  Q4T_CHECK(cache.Bytes() == 0);
  return true;
}

namespace {
bool CancelledExisting(bool restore, bool incremental = false) {
  Fixture f;
  f.data.resize(4096);
  SessionCache cache;
  Q4T_CHECK(cache.Open(f.path, 1, 128 << 20, incremental) &&
            cache.Acquire("same"));
  Q4T_CHECK(cache.Save("same", f.prefix, f.Ranges(), Copy, Active, 4));
  cache.Release("same", true);
  Q4T_CHECK(cache.Acquire("same"));
  if (incremental) {
    // Take the existing entry into Save's local owner, including early exits.
    std::fill(f.data.begin(), f.data.end(), 0);
    int32_t d0 = -1;
    Q4T_CHECK(cache.Restore("same", f.Ranges(), Copy, Active, &d0));
    Q4T_CHECK(d0 == 4 && f.data == std::vector<uint8_t>(4096, 91));
  }
  Q4T_CHECK(kill(cache.Health().worker_pid, SIGSTOP) == 0);
  int copies = 0;
  const auto copy = [&](void* host, void* state, size_t n, bool reading) {
    ++copies;
    return Copy(host, state, n, reading);
  };
  const auto cancelled = [] { return true; };
  const auto start = Clock::now();
  int32_t d0 = -1;
  const auto status = restore
      ? cache.Restore("same", f.Ranges(), copy, cancelled, &d0)
      : cache.Save("same", f.prefix, f.Ranges(), copy, cancelled, 5);
  const auto elapsed = Clock::now() - start;
  std::printf("[CACHE_CANCEL_EXISTING] restore=%d seconds=%.6f status=%s\n",
              restore, std::chrono::duration<double>(elapsed).count(),
              status.message().c_str());
  Q4T_CHECK(!status && status.message().find("cancelled") != std::string::npos);
  Q4T_CHECK(elapsed < 200ms && copies == 0 && d0 == -1);
  cache.Release("same", true);
  Q4T_CHECK(cache.Acquire("same") && !cache.Match("same", f.prefix));
  Q4T_CHECK(SessionCacheTestPeer::Recover(cache));
  Q4T_CHECK(cache.Health().workers_started == 2 && cache.Bytes() == 0);
  return true;
}
}  // namespace

Q4T_TEST(session_cache_cancelled_restore_cleanup_never_waits) {
  return CancelledExisting(true);
}

Q4T_TEST(session_cache_cancelled_replacement_cleanup_never_waits) {
  return CancelledExisting(false);
}

Q4T_TEST(session_cache_cancelled_incremental_cleanup_never_waits) {
  return CancelledExisting(false, true);
}

Q4T_TEST(session_cache_health_is_optional_and_contains_no_private_content) {
  CacheIoHealth health;
  std::printf("[CACHE_HEALTH]%s\n", CacheIoHealthJson(health).c_str());
  q4t::io::Json value;
  Q4T_CHECK(q4t::io::ParseJson(CacheIoHealthJson(health), &value));
  Q4T_CHECK(!value.GetBool("configured") && !value.GetBool("available") &&
            !value.GetBool("readiness_dependency"));
  Fixture f;
  SessionCache cache;
  Q4T_CHECK(cache.Open(f.path));
  const auto text = CacheIoHealthJson(cache.Health());
  std::printf("[CACHE_HEALTH]%s\n", text.c_str());
  Q4T_CHECK(q4t::io::ParseJson(text, &value));
  Q4T_CHECK(value.GetBool("configured") && value.GetBool("available"));
  Q4T_CHECK(!value.GetBool("readiness_dependency") &&
            value.GetInt("buffer_payload_limit_bytes") ==
                3 * CacheIo::kBlockBytes + 4096);
  Q4T_CHECK(text.find(f.path) == std::string::npos && !value.Has("worker_pid"));
  return true;
}
