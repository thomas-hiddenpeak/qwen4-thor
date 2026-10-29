// CPU storage/lease/failure contracts. Run only after the frozen HTTP gate.
#include "q4t/server/session_cache.h"

#include <fcntl.h>
#include <sys/stat.h>
#include <unistd.h>

#include <cerrno>
#include <cstdio>
#include <cstring>
#include <filesystem>
#include <stdexcept>

// Faults affect this CPU executable only, through linker --wrap.
static int fault = 0;
extern "C" ssize_t __real_pwrite(int, const void*, size_t, off_t);
extern "C" ssize_t __wrap_pwrite(int fd, const void* data, size_t n,
                                 off_t off) {
  if (fault == 3) return 0;
  return __real_pwrite(fd, data, n, off);
}

namespace q4t::server {
struct SessionCacheTestPeer {
  static void Expire(SessionCache& c, const std::string& key) {
    c.entries_.at(key)->used -= std::chrono::hours(1);
  }
  static void Truncate(SessionCache& c, const std::string& key) {
    if (ftruncate(c.entries_.at(key)->fd, 0))
      throw std::runtime_error("truncate");
  }
  static void Corrupt(SessionCache& c, const std::string& key) {
    c.entries_.at(key)->digests.at(0).at(0) ^= 1;
  }
};
}  // namespace q4t::server

int main(int argc, char** argv) {
  using namespace q4t;
  using namespace q4t::server;
  int checks = 0;
  auto require = [&](bool passed, const char* message) {
    ++checks;
    if (!passed) throw std::runtime_error(message);
  };
  try {
    require(argc == 2, "storage directory argument");
    const std::string dir = argv[1];
    std::filesystem::create_directory(dir);
    chmod(dir.c_str(), 0700);
    SessionCache cache;
    require(cache.Open(dir).ok(), "direct storage open");
    const auto copy = [](void* host, void* state, size_t bytes, bool restore) {
      std::memcpy(restore ? state : host, restore ? host : state, bytes);
      return Status();
    };
    const auto no_cancel = [] { return false; };
    std::vector<int32_t> prefix(8193, 81234);
    std::vector<uint8_t> data(SessionCache::kStagingBytes + 71);
    for (size_t i = 0; i < data.size(); ++i) data[i] = i * 19 + i / 3;
    const auto expected = data;
    std::vector<SessionRange> ranges{{data.data(), data.size()}};
    auto save = [&](const std::string& key) {
      return cache.Save(key, prefix, ranges, copy, no_cancel, 248000);
    };
    require(cache.Acquire("A"), "first lease");
    require(!cache.Acquire("A"), "same session busy");
    require(save("A").ok(), "save multi-block unaligned range");
    require(cache.Match("A", prefix) == 0, "pending snapshot unpublished");
    cache.Release("A", true);
    require(cache.Acquire("A"), "reacquire");
    require(cache.Match("A", prefix) == 8192, "exact full prefix match");
    auto changed = prefix;
    changed.back() += 1;
    require(cache.Match("A", changed) == 8192,
            "new pending preserves target prefix; caller repairs draft row");
    changed = prefix;
    changed[5] += 1;
    require(cache.Match("A", changed) == 0, "history mismatch");
    require(cache.Match("A", {prefix.data(), 8192}) == 0, "short prompt miss");
    std::fill(data.begin(), data.end(), 0);
    int32_t d0 = -1, pending = -1;
    require(cache.Restore("A", ranges, copy, no_cancel, &d0, &pending).ok(),
            "restore");
    require(data == expected && d0 == 248000 && pending == prefix.back(),
            "complete roundtrip exact");
    SessionCacheTestPeer::Corrupt(cache, "A");
    require(!cache.Restore("A", ranges, copy, no_cancel, &d0), "corruption");
    require(cache.Bytes() == 0, "corrupt entry evicted");
    require(save("A").ok(), "replacement save");
    cache.Release("A", false);
    require(cache.Bytes() == 0, "failed request publishes nothing");
    require(cache.Acquire("A"), "lease after cancel");
    require(!cache.Save(
                "A", prefix, ranges, copy, [] { return true; }, 1),
            "cancel during save");
    require(cache.Bytes() == 0, "cancel leaked no disk budget");
    require(!cache.Save(
                "A", prefix, ranges,
                [](void*, void*, size_t, bool) {
                  return Status::Fail("injected copy failure");
                },
                no_cancel, 1),
            "copy failure");
    require(cache.Bytes() == 0, "copy failure no snapshot");
    require(save("A").ok(), "save before truncated read");
    cache.Release("A", true);
    require(cache.Acquire("A"), "truncated lease");
    SessionCacheTestPeer::Truncate(cache, "A");
    require(!cache.Restore("A", ranges, copy, no_cancel, &d0), "short read");
    require(save("A").ok(), "save before expiry");
    cache.Release("A", true);
    require(cache.Acquire("A"), "expiry lease");
    SessionCacheTestPeer::Expire(cache, "A");
    require(cache.Match("A", prefix) == 0 && cache.Bytes() == 0, "TTL expiry");
    cache.Release("A", false);
    // Keep disk traffic bounded while exercising the real eight-entry LRU.
    ranges[0].bytes = 4097;
    for (int i = 0; i < 9; ++i) {
      const auto key = std::to_string(i);
      require(cache.Acquire(key), "LRU acquire");
      require(save(key).ok(), "LRU save");
      cache.Release(key, true);
    }
    require(cache.Bytes() == 8 * 8192, "entry count/disk padding cap");
    require(cache.Acquire("0"), "LRU miss acquire");
    require(cache.Match("0", prefix) == 0, "oldest evicted");
    cache.Release("0", false);
    for (int injected : {3}) {
      const size_t before = cache.Bytes();
      require(cache.Acquire("fault"), "fault lease");
      fault = injected;
      SessionSaveTiming timing("cpu-fault", "private");
      const auto status =
          cache.Save("fault", prefix, ranges, copy, no_cancel, 248000, &timing);
      timing.Report(status.ok());
      fault = 0;
      require(!status.ok(), "short-write result");
      require(!cache.Match("fault", prefix), "fault unpublished");
      cache.Release("fault", false);
      // The old LRU entry may have been evicted before the failed save.
      require(cache.Bytes() <= before, "failed allocation returned budget");
      require(cache.Acquire("fault"), "fault lease released");
      require(!cache.Match("fault", prefix), "failed request not published");
      cache.Release("fault", false);
    }
    std::printf("PASS checks=%d direct_io=1 staging_bytes=%zu\n", checks,
                SessionCache::kStagingBytes);
    return 0;
  } catch (const std::exception& e) {
    std::fprintf(stderr, "FAIL checks=%d error=%s\n", checks, e.what());
    return 1;
  }
}
