// Optional cache storage: all filesystem calls belong to an exec-only child.
// Requests own no file descriptors and wait only for bounded IPC receipts.
#pragma once

#include <chrono>
#include <cstdint>
#include <functional>
#include <memory>
#include <span>
#include <string>
#include <string_view>

#include "q4t/server/grammar_process.h"
#include "q4t/status.h"

namespace q4t::server {

struct CacheFile {
  uint64_t generation = 0;
  uint64_t id = 0;
};
struct CacheFileStat {
  uint64_t allocated_bytes = 0;
  uint64_t span_bytes = 0;
};
struct CacheIoOptions {
  std::string directory;
  IsolatedWorkerDomain domain = IsolatedWorkerDomain::kSessionCache;
  int timeout_ms = 2000;
  int backoff_ms = 500;
  int restart_window_ms = 60000;
  int restart_limit = 3;
};
struct CacheIoHealth {
  std::string_view state = "disabled";
  bool configured = false;
  bool available = false;
  uint64_t generation = 0;
  uint64_t workers_started = 0;
  uint64_t workers_reaped = 0;
  uint64_t worker_rss_bytes = 0;
  uint64_t controller_failures = 0;
  int workers_active = 0;
  int worker_pid = -1;
};

class CacheIo {
 public:
  static constexpr size_t kBlockBytes = 8 << 20;
  static constexpr size_t kAlignment = 4096;
  static constexpr uint64_t kMaxFileSpan = uint64_t{64} << 30;
  using Cancel = std::function<bool()>;
  explicit CacheIo(CacheIoOptions options);
  ~CacheIo();
  CacheIo(const CacheIo&) = delete;
  CacheIo& operator=(const CacheIo&) = delete;
  Status Initialize(const Cancel& cancel = {});
  Status Create(CacheFile* file, const Cancel& cancel = {});
  Status Read(CacheFile file, uint64_t offset, std::span<uint8_t> output,
              const Cancel& cancel = {});
  Status Write(CacheFile file, uint64_t offset, std::span<const uint8_t> input,
               const Cancel& cancel = {});
  Status Stat(CacheFile file, CacheFileStat* stat, const Cancel& cancel = {});
  void Close(CacheFile file) noexcept;
  CacheIoHealth Health() const;

#ifdef Q4T_GRAMMAR_PROCESS_TESTS
  Status TruncateForTesting(CacheFile file);
  Status FailNextWriteForTesting();
#endif

 private:
  struct State;
  std::shared_ptr<State> state_;
  Status Dispatch(uint64_t operation, CacheFile file, uint64_t offset,
                  std::span<const uint8_t> input, std::span<uint8_t> output,
                  CacheFile* created, CacheFileStat* stat,
                  const Cancel& cancel);
};

int MaybeRunCacheWorker(int argc, char** argv);
#ifdef Q4T_GRAMMAR_PROCESS_TESTS
void DeferCacheSpawnForTesting(bool enabled);
void FailCacheControllerForTesting();
void FailCacheThreadStartForTesting();
void CacheWorkerEntryForTesting(std::string entry);
#endif
}  // namespace q4t::server
