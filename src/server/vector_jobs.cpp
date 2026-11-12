#include "q4t/server/vector_jobs.h"

#include <algorithm>
#include <condition_variable>
#include <ctime>
#include <optional>
#include <thread>

#include "object_protocol.h"
#include "q4t/server/application_owner.h"
#include "q4t/server/responses_owner.h"

namespace q4t::server {
namespace {
using namespace object_detail;
using Clock = std::chrono::steady_clock;
std::string Scope(const ClientIdentity& client) {
  return client.Scoped() ? "client:" + std::string(client.Id())
                         : "local:anonymous";
}
ObjectReply Busy() {
  return Error(503, "vector_executor_unavailable", "Ingestion unavailable");
}
ObjectReply Interrupted() {
  return Error(503, "vector_execution_interrupted",
               "Ingestion execution ended without a confirmed result");
}
bool Gone(const std::string& text) {
  Json owner;
  return text.size() <= 256 && io::ParseJson(text, &owner) &&
         ResponseOwnerStatus(owner) == ResponseOwnerState::kGone;
}
VectorIngestionClaim ClaimFrom(const Json& record) {
  const auto& data = *record.Find("data");
  const auto& job = *data.Find("job");
  const auto& file = *data.Find("file");
  return {file.GetString("vector_store_id"),
          file.GetString("id"),
          record.GetString("id"),
          job.GetString("token"),
          job.GetString("owner"),
          job.GetInt("deadline_at")};
}
bool Removed(const ObjectReply& reply) {
  return reply.http_status == 404 || reply.http_status == 410 ||
         reply.code == "vector_store_expired" ||
         reply.code == "vector_ingestion_claim_conflict";
}
}  // namespace

struct VectorJobs::Job {
  std::string scope, store, file, member;
  size_t scheduling_client = kMaxClients;
  bool active = false, rate_waiting = false;  // State mutex.
  Clock::time_point next;
  size_t errors = 0;
  std::atomic<bool> cancel{false};
  std::shared_ptr<RequestControl> control;  // State mutex.
  // A single selected worker owns everything below. No source text or vector
  // payload is retained while queued or while reconciling an unknown commit.
  ClientIdentity client;
  VectorIngestionClaim claim;
  VectorIndexIdentity identity;
  VectorIngestionResult result;
  ActualUsageCounter usage;
  bool invoked = false;
  std::optional<ObjectReply> outcome;
};
struct VectorJobs::State {
  ObjectStore& objects;
  ClientAdmission& clients;
  const VectorJobsOptions options;
  VectorTextEncoder encode;
  Embedder embed;
  const std::string owner;
  mutable std::mutex mutex;
  std::condition_variable changed;
  std::vector<std::shared_ptr<Job>> jobs;
  std::vector<std::thread> workers;
  std::thread scanner;
  std::atomic<bool> stopping{false}, failed{false};
  size_t cursor = 0, last_client = kMaxClients + 1;
  VectorJobsHealth counters;
  State(ObjectStore& o, ClientAdmission& c, VectorJobsOptions limits,
        VectorTextEncoder encoder, Embedder embedding)
      : objects(o),
        clients(c),
        options(std::move(limits)),
        encode(std::move(encoder)),
        embed(std::move(embedding)),
        owner(OutputJson(CurrentResponseOwner())),
        jobs(options.max_jobs) {}
};

VectorJobs::VectorJobs(ObjectStore& objects, ClientAdmission& clients,
                       VectorJobsOptions options, VectorTextEncoder encode,
                       Embedder embed) {
  if (!options.workers || options.workers > 8 || !options.max_jobs ||
      options.max_jobs > 64 || options.workers > options.max_jobs ||
      !options.max_client_jobs || options.max_client_jobs > options.max_jobs ||
      options.claim_seconds < 1 || options.claim_seconds > 3600 ||
      options.scan_interval.count() < 1 || options.retry_delay.count() < 1 ||
      options.retry_backoff < options.retry_delay ||
      options.retry_backoff > std::chrono::hours(24) || !encode || !embed)
    throw std::invalid_argument("Invalid native ingestion limits");
  state_ = std::make_unique<State>(objects, clients, std::move(options),
                                   std::move(encode), std::move(embed));
  if (state_->owner.size() > 256)
    throw std::runtime_error("Ingestion process identity exceeds limit");
  try {
    for (size_t i = 0; i < state_->options.workers; ++i) {
      ApplicationThreadStart(ApplicationThreadKind::kVectorWorker);
      state_->workers.emplace_back([this] { Worker(); });
    }
    ApplicationThreadStart(ApplicationThreadKind::kVectorScanner);
    state_->scanner = std::thread([this] { Recover(); });
  } catch (...) {
    RequestStop();
    Join();
    throw;
  }
}
VectorJobs::~VectorJobs() {
  RequestStop();
  Join();
}
ObjectReply VectorJobs::Reserve(const std::shared_ptr<Job>& job) {
  const std::lock_guard lock(state_->mutex);
  if (state_->stopping || state_->failed) return Busy();
  size_t client_jobs = 0;
  std::shared_ptr<Job>* free = nullptr;
  for (auto& other : state_->jobs) {
    if (!other) {
      if (!free) free = &other;
    } else if (other->scope == job->scope) {
      ++client_jobs;
      if (other->member == job->member)
        return Error(409, "vector_job_owned", "Ingestion already owned");
    }
  }
  if (!free || client_jobs >= state_->options.max_client_jobs)
    return Error(429, "vector_job_capacity", "Ingestion queue is full");
  *free = job;
  state_->changed.notify_all();
  return {};
}
ObjectReply VectorJobs::Cancel(const BinaryAccess& access,
                               const std::string& store,
                               const std::string& file) {
  auto reply = VectorStores(state_->objects).RequestCancel(access, store, file);
  if (reply.ok() || reply.commit_unknown) {
    const std::lock_guard lock(state_->mutex);
    for (const auto& job : state_->jobs) {
      if (!job || job->scope != access.scope || job->store != store ||
          job->file != file)
        continue;
      job->cancel = true;
      job->next = Clock::now();
      if (job->control) job->control->Cancel();
    }
    state_->changed.notify_all();
  }
  return reply;
}
void VectorJobs::ReleaseHold(const std::shared_ptr<Job>& job) noexcept {
  if (job->result.hold.id.empty() || job->result.hold.released) return;
  try {
    const BinaryAccess access{
        job->scope, Clock::now() + std::chrono::milliseconds(200), {}};
    if (InputSnapshotStore(state_->objects)
            .Release(access, &job->result.hold)
            .ok())
      return;
  } catch (...) {
  }
  state_->objects.RecordAuxiliaryCleanupFailure();
}

ObjectReply VectorJobs::Reconcile(const std::shared_ptr<Job>& job,
                                  const BinaryAccess& access) {
  VectorStores stores(state_->objects);
  if (!job->outcome) return Interrupted();
  auto current = stores.LoadExecution(access, job->store, job->file, job->member);
  if (!current.ok()) return current;
  if (current.value.GetString("id") != job->member)
    return Error(409, "vector_ingestion_claim_conflict",
                 "File membership changed");
  if (current.value.Find("data")->Find("job")->GetBool("cancel_requested"))
    job->cancel = true;
  if (current.value.Find("data")->Find("file")->GetString("status") !=
      "in_progress")
    return {};  // The next owner turn observes the terminal state.
  // A saved original snapshot may be published without a second model call.
  // Finish verifies the actual bytes and current claim/source/membership.
  if (!job->cancel && !job->result.snapshot.id.empty() &&
      job->outcome->commit_unknown) {
    auto result =
        stores.Finish(access, job->claim, job->identity, job->result.snapshot);
    if (result.ok()) {
      job->result.published = true;
      ReleaseHold(job);
      return result;
    }
    if (result.commit_unknown || result.http_status >= 500 ||
        result.http_status == 409)
      return result;
    // A positively absent snapshot, deleted source or expired claim is a
    // finite failure. Never repair it by re-running model computation.
    job->outcome = result;
  }
  const bool invalid = job->outcome->http_status == 400 ||
                       job->outcome->http_status == 413 ||
                       job->outcome->http_status == 404;
  auto result = job->cancel
                    ? stores.Cancel(access, job->claim)
                    : stores.Fail(access, job->claim,
                                  invalid ? "invalid_file" : "server_error",
                                  invalid ? "File cannot be indexed"
                                          : "Ingestion could not complete");
  if (result.ok()) ReleaseHold(job);
  return result;
}

ObjectReply VectorJobs::Turn(const std::shared_ptr<Job>& job, bool* terminal) {
  const BinaryAccess access{
      job->scope, Clock::now() + std::chrono::seconds(2), {}};
  VectorStores stores(state_->objects);
  auto record = stores.LoadExecution(access, job->store, job->file, job->member);
  if (!record.ok()) {
    if (Removed(record)) *terminal = true;
    return record;
  }
  if (record.value.GetString("id") != job->member) {
    *terminal = true;  // A new attachment never inherits the old execution.
    return {};
  }
  const auto& data = *record.value.Find("data");
  const auto& file = *data.Find("file");
  const auto& state = *data.Find("job");
  if (file.GetString("status") != "in_progress") {
    *terminal = true;
    return {};
  }
  if (state.GetBool("cancel_requested")) job->cancel = true;
  if (state.GetString("state") == "running") {
    const auto claim = ClaimFrom(record.value);
    if (job->claim.token != claim.token || job->claim.owner != claim.owner) {
      // Neither elapsed time nor missing monitoring certifies owner death.
      if (!Gone(claim.owner))
        return Error(503, "vector_owner_pending", "Original owner not drained");
      auto result = job->cancel ? stores.Cancel(access, claim)
                                : stores.RequeueAfterExit(access, claim);
      if (result.ok()) {
        const std::lock_guard lock(state_->mutex);
        ++state_->counters.recovered;
      }
      return result;
    }
  } else if (state.GetString("state") == "queued") {
    if (job->invoked || job->outcome)
      return Error(503, "vector_execution_state_changed",
                   "Original invocation cannot be replayed");
    // A missing claim receipt is resolved by this read. Only positively
    // queued work can acquire another claim, before any model invocation.
    return stores.Claim(access, job->store, job->file, state_->owner,
                        state_->options.claim_seconds, &job->claim);
  } else {
    return Error(503, "vector_ingestion_state_invalid", "Invalid job state");
  }
  const auto current = state_->clients.RecoveryIdentity(job->client.Index());
  const bool authorized = current.Allowed() && Scope(current) == job->scope;
  if (authorized) job->client = current;
  if (job->cancel || !authorized ||
      job->claim.deadline_at <= std::time(nullptr)) {
    if (!job->outcome) job->outcome = Interrupted();
  }
  if (job->outcome) return Reconcile(job, access);
  if (state_->stopping) return Busy();
  ClientLease permit;
  if (!permit.Acquire(state_->clients, job->client, ClientLane::kData))
    return Error(429, "vector_rate_wait", "Client concurrency is busy");
  const auto remaining =
      std::max<int64_t>(0, job->claim.deadline_at - std::time(nullptr));
  auto control = std::make_shared<RequestControl>(
      job->claim.token, "", Clock::now() + std::chrono::seconds(remaining),
      job->client.Id());
  {
    const std::lock_guard lock(state_->mutex);
    job->control = control;
    if (job->cancel || state_->stopping) control->Cancel();
  }
  const BinaryAccess execution{job->scope, control->deadline(), [&, control] {
                                 return state_->stopping || job->cancel ||
                                        control->Cancelled();
                               }};
  VectorIngestionPrepared prepared;
  auto result = PrepareVectorIngestion(state_->objects, execution, job->claim,
                                       state_->options.ingestion,
                                       state_->encode, &prepared);
  if (result.ok()) {
    job->identity = prepared.identity;
    // Cached before calling potentially throwing code. All exits thereafter
    // reconcile this invocation, except explicit pre-invocation quota waits.
    job->outcome = Interrupted();
    try {
      result = ExecuteVectorIngestionWithQuota(
          state_->objects, execution, prepared, state_->clients, job->client,
          permit, job->usage,
          [&](const auto& tokens, auto* output) {
            auto live = stores.LoadClaim(execution, job->claim);
            if (!live.ok()) return live;
            if (control->Cancelled() ||
                !state_->clients.OwnsIdentity(job->client))
              return Error(499, "vector_ingestion_cancelled",
                           "Ingestion owner cancelled or revoked");
            if (!job->invoked && !control->ClaimInvocation())
              return Interrupted();
            job->invoked = true;
            return state_->embed(tokens, job->client, permit, control, output);
          },
          &job->result);
    } catch (...) {
      result = Interrupted();
      // The callback contract requires complete unwind/drain on exceptions.
      // A receipt may already exist; reconcile it instead of replaying work.
      result.commit_unknown = !job->result.snapshot.id.empty();
    }
    if (!job->invoked && (result.code == "client_tpm_exhausted" ||
                          result.code == "service_draining")) {
      job->outcome.reset();
      return Error(429, "vector_rate_wait", "Client token admission is busy");
    }
  }
  job->outcome = result.ok() ? Interrupted() : result;
  if (job->result.published) {
    *terminal = true;
    return {};
  }
  // Model/codec owners have returned. Closing state uses a fresh, uncancelled
  // storage deadline so cancellation cannot prevent publication of its end.
  const BinaryAccess close{
      job->scope, Clock::now() + std::chrono::seconds(2), {}};
  return Reconcile(job, close);
}

void VectorJobs::Worker() noexcept {
  try {
    for (;;) {
      std::shared_ptr<Job> job;
      {
        std::unique_lock lock(state_->mutex);
        while (!state_->stopping) {
          const auto now = Clock::now();
          for (int pass = 0; pass < 2 && !job; ++pass) {
            for (size_t n = 0; n < state_->jobs.size(); ++n) {
              const size_t i = (state_->cursor + n) % state_->jobs.size();
              const auto& candidate = state_->jobs[i];
              if (!candidate || candidate->active || candidate->next > now ||
                  (!pass &&
                   candidate->scheduling_client == state_->last_client))
                continue;
              job = candidate;
              job->active = true;
              state_->cursor = (i + 1) % state_->jobs.size();
              state_->last_client = job->scheduling_client;
              break;
            }
          }
          if (job) break;
          state_->changed.wait_for(lock, std::chrono::milliseconds(25));
        }
        if (state_->stopping) return;
      }
      bool terminal = false;
      ObjectReply result;
      try {
        result = Turn(job, &terminal);
      } catch (...) {
        result = Busy();
        if (job->invoked && !job->outcome) job->outcome = Interrupted();
      }
      if (terminal) ReleaseHold(job);
      {
        const std::lock_guard lock(state_->mutex);
        ++state_->counters.turns;
        job->active = false;
        job->control.reset();
        job->rate_waiting = result.code == "vector_rate_wait";
        if (terminal) {
          for (auto& slot : state_->jobs)
            if (slot == job) slot.reset();
        } else if (job->rate_waiting) {
          ++state_->counters.rate_deferrals;
          job->next = Clock::now() + state_->options.retry_delay;
        } else if (!result.ok()) {
          ++state_->counters.errors;
          ++job->errors;
          job->next = Clock::now() + (job->errors % 3 == 0
                                          ? state_->options.retry_backoff
                                          : state_->options.retry_delay);
        } else {
          job->errors = 0;
          job->next = Clock::now();
        }
      }
      state_->changed.notify_all();
    }
  } catch (...) {
    state_->failed = true;
    RequestStop();
  }
}

void VectorJobs::Recover() noexcept {
  try {
    std::string cursor;
    size_t pending = 0, errors = 0;
    while (!state_->stopping) {
      const auto cancelled = [&] { return state_->stopping.load(); };
      try {
        auto page = state_->objects.ScanVectorFiles(
            cursor, Clock::now() + std::chrono::seconds(2), cancelled);
        if (!page.ok()) throw std::runtime_error("Ingestion scan unavailable");
        const auto* rows = page.value.GetArray("data");
        if (!rows) throw std::runtime_error("Invalid ingestion recovery page");
        for (const auto& row : rows->array) {
          if (state_->stopping) break;
          bool owned = false;
          const auto scope = row.GetString("scope"), id = row.GetString("id");
          {
            const std::lock_guard lock(state_->mutex);
            for (const auto& job : state_->jobs) {
              if (!job || job->scope != scope || job->member != id) continue;
              owned = true;
              if (row.GetBool("cancel_requested")) {
                job->cancel = true;
                if (job->control) job->control->Cancel();
              }
              break;
            }
          }
          if (owned) continue;
          if (scope.empty() || (row.GetString("state") != "queued" &&
                                !Gone(row.GetString("owner")))) {
            ++pending;
            continue;
          }
          auto job = std::make_shared<Job>();
          job->scope = scope;
          job->member = id;
          job->store = row.GetString("store_id");
          job->file = row.GetString("file_id");
          job->cancel = row.GetBool("cancel_requested");
          const size_t count = state_->clients.Enabled()
                                   ? state_->clients.Size()
                                   : kMaxClients + 1;
          for (size_t i = 0; i < count; ++i) {
            auto identity = state_->clients.RecoveryIdentity(i);
            if (identity.Allowed() && Scope(identity) == scope) {
              job->client = identity;
              job->scheduling_client = identity.Index();
              break;
            }
          }
          // Revoked clients' records must still reach a terminal failure.
          if (!Reserve(job).ok()) ++pending;
        }
        cursor = page.value.GetString("cursor");
        if (!page.value.GetBool("has_more")) {
          cursor.clear();
          const std::lock_guard lock(state_->mutex);
          state_->counters.recovery_scanned = true;
          state_->counters.recovery_pending = pending;
          pending = 0;
        }
        errors = 0;
      } catch (...) {
        ++errors;
        const std::lock_guard lock(state_->mutex);
        ++state_->counters.recovery_errors;
      }
      std::unique_lock lock(state_->mutex);
      state_->changed.wait_for(lock,
                               errors && errors % 3 == 0
                                   ? state_->options.retry_backoff
                                   : state_->options.scan_interval,
                               cancelled);
    }
  } catch (...) {
    const std::lock_guard lock(state_->mutex);
    ++state_->counters.recovery_errors;
  }
}
void VectorJobs::RequestStop() noexcept {
  if (!state_) return;
  state_->stopping = true;
  const std::lock_guard lock(state_->mutex);
  for (const auto& job : state_->jobs)
    if (job && job->control)
      job->control->Cancel(RequestControl::State::kShutdown);
  state_->changed.notify_all();
}
void VectorJobs::Join() {
  if (!state_) return;
  if (state_->scanner.joinable()) state_->scanner.join();
  for (auto& worker : state_->workers)
    if (worker.joinable()) worker.join();
}
VectorJobsHealth VectorJobs::Health() const {
  const std::lock_guard lock(state_->mutex);
  auto result = state_->counters;
  for (const auto& job : state_->jobs) {
    if (!job) continue;
    ++result.owned;
    result.active += job->active;
    result.rate_waiting += job->rate_waiting;
  }
  result.stopping = state_->stopping;
  result.failed = state_->failed;
  return result;
}
std::string VectorJobsHealthJson(const VectorJobsHealth& health,
                                 bool available) {
  return OutputJson(
      Object({{"state", String(!available        ? "unavailable"
                               : health.failed   ? "failed"
                               : health.stopping ? "stopping"
                                                 : "ready")},
              {"readiness_dependency", Boolean(false)},
              {"owned", Number(health.owned)},
              {"active", Number(health.active)},
              {"rate_waiting", Number(health.rate_waiting)},
              {"rate_deferrals", Number(health.rate_deferrals)},
              {"turns", Number(health.turns)},
              {"errors", Number(health.errors)},
              {"recovered", Number(health.recovered)},
              {"recovery_errors", Number(health.recovery_errors)},
              {"recovery_pending", Number(health.recovery_pending)},
              {"recovery_scanned", Boolean(health.recovery_scanned)}}));
}
}  // namespace q4t::server
