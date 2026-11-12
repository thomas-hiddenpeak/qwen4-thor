// Native owned jobs, with bounded workers and serialized input/plan payloads.
// This byte budget is not a measurement of allocator overhead or total DRAM;
// normal runtime memory admission remains required before model execution.
#pragma once

#include <functional>
#include <memory>
#include <optional>
#include <string_view>

#include "q4t/server/client_policy.h"
#include "q4t/server/batch_budget.h"
#include "q4t/server/request_limits.h"
#include "q4t/server/responses_execution.h"

namespace q4t::server {

struct ResponsesJobsOptions {
  size_t workers = 1;
  size_t max_jobs = 8;
  size_t max_client_jobs = 8;
  size_t max_input_bytes = kBackgroundInputBytes;
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
  bool recovery_configured = false;
  bool recovery_scanned = false;
  uint64_t recovered = 0;
  uint64_t interrupted = 0;
  uint64_t recovery_errors = 0;
  size_t recovery_pending = 0;
  size_t rate_waiting = 0;
  uint64_t rate_deferrals = 0;
  size_t foreground_owned = 0;
  size_t foreground_active = 0;
  size_t foreground_pending = 0;
  uint64_t foreground_reconciled = 0;
  uint64_t foreground_errors = 0;
};

class ResponsesJobs : public IResponsesCleanup {
 public:
  struct Job {
    std::unique_ptr<ResponsesExecution> run;
    ClientIdentity client;
    std::optional<ClientLease> permit{std::in_place};
    std::shared_ptr<RequestControl> control;
    std::shared_ptr<ClientRateLease> rate;
    std::optional<BatchRateBudget> planned_rate;
    ClientIdentity planned_client;

   private:
    friend class ResponsesJobs;
    enum class Stage { kPreparing, kQueued, kRunning, kClosing, kUncertain };
    Stage stage = Stage::kPreparing;
    size_t bytes = 0;
    size_t recovery_attempts = 0;
    std::chrono::steady_clock::time_point next_recovery;
    std::chrono::steady_clock::time_point next_rate;
    bool rate_waiting = false;
  };
  // Called on a worker thread after the queued->in_progress durable receipt.
  // It must drain model/grammar resources before returning or throwing.
  using Executor = std::function<void(Job&)>;
  // Before durable StartRunning, for Batch-origin jobs whose process-local
  // precharge was lost. A wait keeps the original queued record/deadline.
  // No model is allowed here. Success must supply scoped rate/data leases.
  using RateAdmission = std::function<ObjectReply(Job&)>;
  ResponsesJobs(ObjectStore& objects, ResponsesJobsOptions options,
                Executor executor, RateAdmission rate_admission = {});
  ~ResponsesJobs();
  ResponsesJobs(const ResponsesJobs&) = delete;
  ResponsesJobs& operator=(const ResponsesJobs&) = delete;
  // Transfer the already admitted client's permit after durable acceptance.
  // The callback is invoked once, on the caller thread, before execution.
  using TransferPermit = std::function<bool(ClientLease&)>;
  ObjectReply Submit(std::unique_ptr<ResponsesExecution> run,
                     ClientIdentity client, const TransferPermit& transfer,
                     std::shared_ptr<ClientRateLease> rate = {});
  // Cancel returns a real persisted terminal response. A deadline can end the
  // caller's wait without undoing cancellation or releasing a running owner.
  ObjectReply Cancel(const std::string& id, const StateAccess& access);
  // One bounded scanner, independent of request/model workers. Client policy
  // must remain immutable and outlive the manager. No process is signalled.
  void StartRecovery(ClientAdmission& clients, ChatPlanConfig config);
  // Private Batch observation: signals cancellation without blocking a row
  // worker. Only a matching terminal whose executor has drained is returned.
  ObjectReply ObserveBatch(const BatchResponseLink& link,
                           const StateAccess& access, bool cancel);
  void RequestStop() noexcept;
  void Join();
  ResponsesJobsHealth Health() const;
  // Fixed metadata slots, separately bounded by max_jobs/max_client_jobs.
  // No prompt, executor callback, request fd or client permit is retained.
  ObjectReply Reserve(const std::string& scope, const std::string& id,
                      uint64_t* ticket) override;
  void Close(uint64_t ticket, bool known_terminal) noexcept override;

 private:
  struct State;
  std::unique_ptr<State> state_;
  ObjectReply Adopt(std::unique_ptr<ResponsesExecution> run,
                    ClientIdentity client, const TransferPermit& transfer,
                    bool restored, std::shared_ptr<ClientRateLease> rate = {});
  void Recover(ClientAdmission& clients, ChatPlanConfig config) noexcept;
  void Worker() noexcept;
  void Execute(const std::shared_ptr<Job>& job) noexcept;
  void Reconcile(const std::shared_ptr<Job>& job) noexcept;
  void ReconcileForeground(uint64_t ticket, std::string_view scope,
                           std::string_view id) noexcept;
  void Release(const std::shared_ptr<Job>& job, bool known_terminal);
};
using BackgroundRatePlanner =
    std::function<ObjectReply(ResponsesExecution&, const ChatPlanConfig&,
                              const BinaryAccess&, BatchRateBudget*)>;
// Shared native admission for recovered Batch background jobs. The supplied
// planner uses original restored inputs only; no model or public publication.
ObjectReply AdmitBackgroundBatchRate(ClientAdmission& clients,
                                     ResponsesJobs::Job& job,
                                     ChatPlanConfig config,
                                     const BackgroundRatePlanner& planner);
}  // namespace q4t::server
