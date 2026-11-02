// Linux exec-only isolation. No library/CUDA code runs in a forked address
// space. The child owns no server descriptors or model allocations.
#pragma once

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

enum class GrammarFailure { kNone, kCancelled, kTimeout, kResource, kWorker };

struct GrammarWorkerReport {
  uint64_t max_rss_bytes =
      0;                // wait4 ru_maxrss, not the server/whole-machine RAM
  int exit_status = 0;  // raw wait status, includes deliberate cleanup kill
  bool reaped = false;
};

struct GrammarControl {
  std::function<bool()> cancelled;
  std::function<void()> worker_started;
  std::function<void()> compile_started;
  std::function<void(const GrammarWorkerReport&)> worker_finished;
  std::chrono::steady_clock::time_point compile_deadline =
      std::chrono::steady_clock::now() +
      std::chrono::milliseconds(kGrammarCompileMilliseconds);
  // Internal callers may tighten, never raise, the production ceiling.
  uint64_t address_space_bytes = kGrammarAddressSpaceLimit;
};

namespace detail {

// Single owner. A signal always targets its pidfd; reap precedes lease release.
class GrammarProcess {
 public:
  ~GrammarProcess();
  GrammarProcess(const GrammarProcess&) = delete;
  GrammarProcess& operator=(const GrammarProcess&) = delete;
  static Status Start(const char* entry, const GrammarControl& control,
                      std::unique_ptr<GrammarProcess>* out);
  Status Transfer(void* data, size_t bytes, bool write,
                  std::chrono::steady_clock::time_point deadline);
  GrammarFailure Failure() const { return failure_; }
  void Stop();

 private:
  explicit GrammarProcess(GrammarControl control);
  GrammarControl control_;
  int socket_ = -1;
  int pid_ = -1;
  int pid_fd_ = -1;
  GrammarFailure failure_ = GrammarFailure::kNone;
};

// Shared child entry validation: exact inherited UNIX socket, parent death,
// fixed-or-tighter rlimits, no core dumps. False means exit without compiling.
bool EnterGrammarWorker(int argc, char** argv);
bool WorkerTransfer(void* data, size_t bytes, bool write);

}  // namespace detail
}  // namespace q4t::server
