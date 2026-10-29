#pragma once

#include <chrono>
#include <functional>
#include <map>
#include <memory>
#include <mutex>
#include <set>
#include <span>
#include <string>
#include <vector>

#include "q4t/status.h"
#include "q4t/server/session_save_timing.h"
#include "q4t/trace/sha256.h"

namespace q4t::server {

struct SessionRange {
  void* data;
  size_t bytes;
};

// Same-process, capability-keyed prefix snapshots. Device copies are supplied
// by the caller, which must own the model scratch/slot until Transfer returns.
// CPU contracts use the same storage path with a checked host-copy callback.
class SessionCache {
 public:
  static constexpr size_t kStagingBytes = 8 << 20;
  static constexpr size_t kRamBudget = 32 << 20;
  static constexpr size_t kDiskBudget = size_t{24} << 30;
  static constexpr size_t kMaxEntries = 8;
  static constexpr int kMinPrefix = 8192;
  static constexpr int kMaxPrefix = 262144;
  using Copy = std::function<Status(void* host, void* state, size_t bytes,
                                    bool restore)>;
  using Cancel = std::function<bool()>;

  SessionCache() = default;
  ~SessionCache();
  SessionCache(const SessionCache&) = delete;
  SessionCache& operator=(const SessionCache&) = delete;
  Status Open(const std::string& directory, int min_prefix = kMinPrefix,
              size_t disk_budget = kDiskBudget, bool incremental = false);
  bool Enabled() const { return directory_fd_ >= 0; }
  bool Acquire(const std::string& key);
  // Shared snapshots use scope + ':' + SHA256(prefix) as the key. A selected
  // entry is leased until Release, including the interval before Restore.
  bool AcquireNew(const std::string& key);
  int MatchShared(const std::string& scope, std::span<const int32_t> prompt,
                  std::string* key);
  void Release(const std::string& key, bool success);
  int Match(const std::string& key, std::span<const int32_t> prompt);
  Status Restore(const std::string& key, std::span<const SessionRange> ranges,
                  const Copy& copy, const Cancel& cancel, int32_t* d0,
                  int32_t* pending = nullptr);
  Status Save(const std::string& key, std::span<const int32_t> prefix,
               std::span<const SessionRange> ranges, const Copy& copy,
               const Cancel& cancel, int32_t d0,
               SessionSaveTiming* timing = nullptr);
  size_t Bytes() const;

 private:
  friend struct SessionCacheTestPeer;
  struct Entry {
    int fd = -1;
    size_t bytes = 0;
    size_t disk_charge = 0;
    bool published = false;
    bool restored = false;
    // Incremental files reserve sparse 8 MiB address slots. Only written
    // extents consume disk space; slots never alias or move between ranges.
    size_t next_offset = 0;
    std::vector<size_t> offsets;
    int32_t d0 = -1;
    std::vector<int32_t> prefix;  // Includes shifted draft's pending token.
    std::vector<size_t> sizes;
    std::vector<trace::Digest> digests;
    std::chrono::steady_clock::time_point used;
    ~Entry();
  };
  using Entries = std::map<std::string, std::unique_ptr<Entry>>;
  void Erase(Entries::iterator it);
  Status SaveIncremental(Entry* entry, std::span<const SessionRange> ranges,
                         const Copy& copy, const Cancel& cancel,
                         SessionSaveTiming* timing);
  Status Transfer(Entry* entry, std::span<const SessionRange> ranges,
                   const Copy& copy, const Cancel& cancel, bool restore,
                   SessionSaveTiming* timing = nullptr);
  mutable std::mutex mutex_;
  int directory_fd_ = -1;
  void* staging_ = nullptr;
  size_t bytes_ = 0;
  int min_prefix_ = kMinPrefix;
  size_t disk_budget_ = kDiskBudget;
  bool incremental_ = false;
  Entries entries_;
  std::set<std::string> busy_;
};

// Construct before slot admission. Every exit releases the same-session lease;
// only successful HTTP completion publishes a newly saved snapshot.
struct SessionLease {
  SessionCache* cache = nullptr;
  std::string key;
  bool success = false;
  ~SessionLease() { if (cache) cache->Release(key, success); }
};

}  // namespace q4t::server
