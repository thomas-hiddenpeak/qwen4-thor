// Native Batch lifecycle owner. Queue ownership is reserved before publication;
// each turn validates or executes at most one row. No process is signalled.
#pragma once

#include "q4t/server/batch_execution.h"
#include "q4t/server/batch_validation.h"

namespace q4t::server {
struct BatchJobsOptions {
  size_t workers = 1;
  size_t max_jobs = 8;
  size_t max_client_jobs = 4;
  size_t max_result_bytes = 256ULL << 20;
  std::chrono::milliseconds turn_timeout{600000};
  std::chrono::milliseconds retry_delay{250};
  std::chrono::milliseconds retry_backoff{60000};
  std::chrono::milliseconds scan_interval{1000};
};
struct BatchJobsHealth {
  size_t owned = 0, active = 0, reserved_result_bytes = 0;
  size_t rate_waiting = 0;
  uint64_t rate_deferrals = 0;
  uint64_t turns = 0, recovered = 0, interrupted = 0, errors = 0;
  uint64_t recovery_errors = 0, recovery_pending = 0;
  bool stopping = false, failed = false, recovery_scanned = false;
};
class BatchJobs {
 public:
  // Must return only after all synchronous model/grammar work has drained.
  // A background Response transfers data/rate ownership into its existing
  // owner. The queued receipt is not a completed Batch row.
  using Executor = std::function<BatchRowResult(
      const std::string&, const BatchInputRow&, const io::Json&,
      const ClientIdentity&, ClientLease&,
      const std::shared_ptr<RequestControl>&,
      std::shared_ptr<ClientRateLease>)>;
  using Continuation = std::function<ObjectReply(
      const BatchResponseLink&, const StateAccess&, bool cancel)>;
  BatchJobs(ObjectStore& objects, ClientAdmission& clients,
            ChatPlanConfig config, BatchJobsOptions options, Executor execute,
            BatchInputMeter meter = {}, BatchRatePlanner planner = {},
            Continuation continuation = {});
  ~BatchJobs();
  BatchJobs(const BatchJobs&) = delete;
  BatchJobs& operator=(const BatchJobs&) = delete;
  ObjectReply Submit(const BinaryAccess& access, const BatchCreate& options,
                     const ClientIdentity& client);
  ObjectReply Cancel(const BinaryAccess& access, const std::string& id);
  void RequestStop() noexcept;
  void Join();
  BatchJobsHealth Health() const;

 private:
  struct Job;
  struct State;
  std::unique_ptr<State> state_;
  ObjectReply Reserve(const std::shared_ptr<Job>& job);
  void Worker() noexcept;
  void Recover() noexcept;
  ObjectReply Turn(const std::shared_ptr<Job>& job, bool* terminal);
  ObjectReply RunRow(const std::shared_ptr<Job>& job, const io::Json& data,
                     const BinaryAccess& access);
  ObjectReply SaveResult(const std::shared_ptr<Job>& job,
                         const BinaryAccess& access);
  ObjectReply FollowResponse(const std::shared_ptr<Job>& job,
                             const io::Json& data, const io::Json& row,
                             const BinaryAccess& access);
  void ReleaseResult(const std::shared_ptr<Job>& job, bool keep_ticket = false);
};
}  // namespace q4t::server
