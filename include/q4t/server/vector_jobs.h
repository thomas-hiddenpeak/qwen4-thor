// Bounded native ingestion workers. Persisted membership is the queue; no
// external daemon/database and no model restart on auxiliary failure.
#pragma once

#include "q4t/server/request_control.h"
#include "q4t/server/vector_embedding_execution.h"

namespace q4t::server {
struct VectorJobsOptions {
  size_t workers = 1, max_jobs = 8, max_client_jobs = 4;
  int claim_seconds = 600;
  std::chrono::milliseconds scan_interval{1000};
  std::chrono::milliseconds retry_delay{250}, retry_backoff{60000};
  VectorIngestionOptions ingestion;
};
struct VectorJobsHealth {
  size_t owned = 0, active = 0, rate_waiting = 0;
  uint64_t turns = 0, recovered = 0, errors = 0, recovery_errors = 0;
  uint64_t recovery_pending = 0, rate_deferrals = 0;
  bool stopping = false, failed = false, recovery_scanned = false;
};
std::string VectorJobsHealthJson(const VectorJobsHealth& health,
                                 bool available);
class VectorJobs {
 public:
  // Each call is one chunk in a single owned invocation. Return/throw only
  // after all native model work and its slot/workspace have drained.
  using Embedder = std::function<ObjectReply(
      const std::vector<int32_t>&, const ClientIdentity&, ClientLease&,
      const std::shared_ptr<RequestControl>&, EmbeddingStepResult*)>;
  VectorJobs(ObjectStore& objects, ClientAdmission& clients,
             VectorJobsOptions options, VectorTextEncoder encode,
             Embedder embed);
  ~VectorJobs();
  VectorJobs(const VectorJobs&) = delete;
  VectorJobs& operator=(const VectorJobs&) = delete;
  // Durable intent is acknowledged, never a false claim of GPU drain.
  ObjectReply Cancel(const BinaryAccess& access, const std::string& store,
                     const std::string& file);
  void RequestStop() noexcept;
  void Join();
  VectorJobsHealth Health() const;

 private:
  struct Job;
  struct State;
  std::unique_ptr<State> state_;
  ObjectReply Reserve(const std::shared_ptr<Job>& job);
  ObjectReply Turn(const std::shared_ptr<Job>& job, bool* terminal);
  ObjectReply Reconcile(const std::shared_ptr<Job>& job,
                        const BinaryAccess& access);
  void ReleaseHold(const std::shared_ptr<Job>& job) noexcept;
  void Worker() noexcept;
  void Recover() noexcept;
};
}  // namespace q4t::server
