// Linux exec-only isolation. No library/CUDA code runs in a forked address
// space. The child owns no server descriptors or model allocations.
#pragma once

#include <atomic>
#include <chrono>
#include <cstdint>
#include <functional>
#include <memory>
#include <span>
#include <string>

#include "q4t/status.h"

namespace q4t::server {

inline constexpr uint64_t kGrammarAddressSpaceLimit = 2ULL * 1024 * 1024 * 1024;
inline constexpr int kGrammarCompileMilliseconds = 10000;
inline constexpr int kGrammarStepMilliseconds = 2000;
inline constexpr int kGrammarCleanupMilliseconds = 100;

enum class GrammarFailure {
  kNone,
  kCancelled,
  kTimeout,
  kResource,
  kWorker,
  kUnavailable
};

// Independent lifetime: deferred cleanup never retains a server/request
// callback.
struct GrammarWorkerCounters {
  std::atomic<int> active{0};
  std::atomic<uint64_t> started{0};
  std::atomic<uint64_t> reaped{0};
  std::atomic<uint64_t> max_rss_bytes{0};
  std::atomic<uint64_t> reported_rss_bytes{0};
  std::atomic<uint64_t> rss_reports{0};
};

struct GrammarWorkerReport {
  // wait4 includes pre-exec inherited mm peaks; do not compare with RLIMIT_AS.
  uint64_t max_rss_bytes = 0;
  // Last reported post-exec VmHWM. Zero means unknown, not zero consumption.
  uint64_t reported_rss_bytes = 0;
  int exit_status = 0;  // raw wait status, includes deliberate cleanup kill
  bool reaped = false;
};

struct GrammarControl {
  std::shared_ptr<GrammarWorkerCounters> counters;
  std::function<bool()> cancelled;
  std::function<void()> worker_started;
  std::function<void()> worker_ready;
  std::function<void()> compile_started;
  // Called once before Stop returns; reaped=false means deferred ownership.
  // This callback is never saved by the deferred process owner.
  std::function<void(const GrammarWorkerReport&)> worker_finished;
  std::chrono::steady_clock::time_point compile_deadline =
      std::chrono::steady_clock::now() +
      std::chrono::milliseconds(kGrammarCompileMilliseconds);
  // Internal callers may tighten, never raise, the production ceiling.
  uint64_t address_space_bytes = kGrammarAddressSpaceLimit;
};

namespace detail {

// Called by the native event loop and before admission. No wait or callback.
void PollGrammarWorkers();
bool GrammarCleanupPending();

#ifdef Q4T_GRAMMAR_PROCESS_TESTS
// Host-only fixture: simulate unconfirmed exit without kernel D-state.
void DeferGrammarReapingForTesting(bool enabled);
#endif

// Single process-wide slot. Unreaped cleanup keeps that slot after request
// exit.
class GrammarProcess {
 public:
  ~GrammarProcess();
  GrammarProcess(const GrammarProcess&) = delete;
  GrammarProcess& operator=(const GrammarProcess&) = delete;
  static Status Start(const char* entry, const GrammarControl& control,
                      std::unique_ptr<GrammarProcess>* out,
                      GrammarFailure* failure = nullptr);
  Status Transfer(void* data, size_t bytes, bool write,
                  std::chrono::steady_clock::time_point deadline);
  GrammarFailure Failure() const { return failure_; }
  void ReportRss(uint64_t bytes);
  void Stop();

 private:
  explicit GrammarProcess(GrammarControl control);
  GrammarControl control_;
  int socket_ = -1;
  int pid_ = -1;
  int pid_fd_ = -1;
  GrammarFailure failure_ = GrammarFailure::kNone;
  uint64_t reported_rss_bytes_ = 0;
  bool slot_owned_ = false;
};

// Shared child entry validation: exact inherited UNIX socket, parent death,
// fixed-or-tighter rlimits, no core dumps. False means exit without compiling.
bool EnterGrammarWorker(int argc, char** argv);
bool WorkerTransfer(void* data, size_t bytes, bool write);

}  // namespace detail
}  // namespace q4t::server
