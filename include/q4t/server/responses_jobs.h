// Native owned jobs, with bounded workers and serialized input/plan payloads.
// This byte budget is not a measurement of allocator overhead or total DRAM;
// normal runtime memory admission remains required before model execution.
#pragma once

#include <functional>
#include <memory>

#include "q4t/server/client_policy.h"
#include "q4t/server/responses_execution.h"

namespace q4t::server {

struct ResponsesJobsOptions {
  size_t workers = 1;
  size_t max_jobs = 8;
  size_t max_client_jobs = 8;
  size_t max_input_bytes = 64ULL << 20;
  std::chrono::milliseconds timeout{600000};
};

struct ResponsesJobsHealth {
  size_t owned = 0;
  size_t queued = 0;
  size_t running = 0;
  size_t uncertain = 0;
  size_t input_bytes = 0;
  bool stopping = false;
  bool failed = false;
};

class ResponsesJobs {
 public:
  struct Job {
    std::unique_ptr<ResponsesExecution> run;
    ClientIdentity client;
    ClientLease permit;
    std::shared_ptr<RequestControl> control;

   private:
    friend class ResponsesJobs;
    enum class Stage { kPreparing, kQueued, kRunning, kClosing, kUncertain };
    Stage stage = Stage::kPreparing;
    size_t bytes = 0;
    size_t recovery_attempts = 0;
    std::chrono::steady_clock::time_point next_recovery;
  };
  // Called on a worker thread after the queued->in_progress durable receipt.
  // It must drain model/grammar resources before returning or throwing.
  using Executor = std::function<void(Job&)>;
  ResponsesJobs(ObjectStore& objects, ResponsesJobsOptions options,
                Executor executor);
  ~ResponsesJobs();
  ResponsesJobs(const ResponsesJobs&) = delete;
  ResponsesJobs& operator=(const ResponsesJobs&) = delete;
  // Transfer the already admitted client's permit after durable acceptance.
  // The callback is invoked once, on the caller thread, before execution.
  using TransferPermit = std::function<bool(ClientLease&)>;
  ObjectReply Submit(std::unique_ptr<ResponsesExecution> run,
                     ClientIdentity client, const TransferPermit& transfer);
  // Cancel returns a real persisted terminal response. A deadline can end the
  // caller's wait without undoing cancellation or releasing a running owner.
  ObjectReply Cancel(const std::string& id, const StateAccess& access);
  void RequestStop() noexcept;
  void Join();
  ResponsesJobsHealth Health() const;

 private:
  struct State;
  std::unique_ptr<State> state_;
  void Worker() noexcept;
  void Execute(const std::shared_ptr<Job>& job) noexcept;
  void Reconcile(const std::shared_ptr<Job>& job) noexcept;
  void Release(const std::shared_ptr<Job>& job, bool known_terminal);
};
}  // namespace q4t::server
