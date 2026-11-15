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
#include "q4t/server/worker_io_budget.h"

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
  kDependency,
  kWorker,
  kUnavailable
};

// Independent fixed-capacity domains: a blocked object-store worker must not
// consume the grammar slot or degrade ordinary structured generation.
enum class IsolatedWorkerDomain {
  kGrammar,
  kObjects,
  kSessionCache,
  kPrefixCache,
  kFileFetch,
  kFileParse,
  kEmbeddingAssets,
  kToolResults,
  kPrograms
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
  IsolatedWorkerDomain domain = IsolatedWorkerDomain::kGrammar;
};

namespace detail {

class WorkerIoBroker;

// Called by the native event loop and before admission. No wait or callback.
void PollGrammarWorkers();
// Native loop polling also collects independent one-shot file fetches, even
// when no later request uses that capability. No request callback is retained.
void PollRequestWorkers();
bool GrammarCleanupPending();
void PollIsolatedWorkers(IsolatedWorkerDomain domain);
bool IsolatedCleanupPending(IsolatedWorkerDomain domain);

#ifdef Q4T_GRAMMAR_PROCESS_TESTS
// Host-only fixture: simulate unconfirmed exit without kernel D-state.
void DeferGrammarReapingForTesting(bool enabled);
#endif

// One process-wide slot per fixed domain. Unreaped cleanup keeps the same
// domain's slot after request exit. Existing grammar users stay in kGrammar.
class GrammarProcess {
 public:
  ~GrammarProcess();
  GrammarProcess(const GrammarProcess&) = delete;
  GrammarProcess& operator=(const GrammarProcess&) = delete;
  static Status Start(const char* entry, const GrammarControl& control,
                      std::unique_ptr<GrammarProcess>* out,
                      GrammarFailure* failure = nullptr);
  Status Transfer(void* data, size_t bytes, bool write,
                  std::chrono::steady_clock::time_point deadline,
                  const std::function<bool()>& cancelled = {});
  // Receive exactly one kernel listener from this owned child. Its resource
  // requests are serviced by Transfer/CollectOutput and confirmed exit waits.
  Status ReceiveIoBudget(const WorkerIoBudget& budget,
                         std::chrono::steady_clock::time_point deadline,
                         const std::function<bool()>& cancelled = {});
  // One-shot raw stdout: publish only after EOF AND a confirmed normal zero
  // exit. Failure clears output, stops this child and retains an unreaped slot.
  Status CollectOutput(std::string* output, size_t max_bytes,
                       std::chrono::steady_clock::time_point deadline,
                       const std::function<bool()>& cancelled = {});
  GrammarFailure Failure() const { return failure_; }
  // Identity of this still-owned direct child; never used after Stop.
  int Pid() const { return pid_; }
  void ReportRss(uint64_t bytes);
  void Stop();

 private:
  explicit GrammarProcess(GrammarControl control);
  Status AwaitExit(std::chrono::steady_clock::time_point deadline,
                   const std::function<bool()>& cancelled);
  bool PumpIoBudget();
  GrammarControl control_;
  int socket_ = -1;
  int pid_ = -1;
  int pid_fd_ = -1;
  GrammarFailure failure_ = GrammarFailure::kNone;
  uint64_t reported_rss_bytes_ = 0;
  bool slot_owned_ = false;
  std::unique_ptr<WorkerIoBroker> io_broker_;
};

// Shared child entry validation: exact inherited UNIX socket, parent death,
// fixed-or-tighter rlimits, no core dumps. False means exit without compiling.
bool EnterGrammarWorker(int argc, char** argv);
bool WorkerTransfer(void* data, size_t bytes, bool write);

}  // namespace detail
}  // namespace q4t::server
