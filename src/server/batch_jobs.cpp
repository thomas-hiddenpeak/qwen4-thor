#include "q4t/server/batch_jobs.h"

#include <algorithm>
#include <atomic>
#include <condition_variable>
#include <ctime>
#include <mutex>
#include <optional>
#include <thread>

#include "object_protocol.h"
#include "q4t/server/lifetime.h"
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
  return Error(503, "batch_executor_unavailable", "Batch executor unavailable");
}
ObjectReply Interrupted() {
  return Error(503, "batch_execution_interrupted",
               "Prior execution drained without a confirmed result; "
               "started work is not replayed");
}
}  // namespace

struct BatchJobs::Job {
  // Identity and slot exist before any durable create or takeover.
  std::string id, scope;
  ClientIdentity client, validation_client;
  size_t scheduling_client = kMaxClients;
  bool submitting = true, active = false;
  std::atomic<bool> cancel{false};
  Clock::time_point next;
  size_t attempts = 0;
  std::shared_ptr<RequestControl> control;  // state mutex
  // The remaining fields are exclusively owned by the selected worker.
  std::unique_ptr<BatchValidation> validation;
  ObjectReply validation_error;
  std::optional<BatchRowResult> result;
  size_t result_index = 0;
  std::string custom_id;
  bool result_reserved = false;  // state mutex
  // This exact in-memory owner started the row and has returned from its
  // callback (or never called it). A claimed root is not equivalent proof.
  std::optional<size_t> locally_drained;
  std::string local_ticket;
};
struct BatchJobs::State {
  ObjectStore& objects;
  ClientAdmission& clients;
  ChatPlanConfig config;
  BatchJobsOptions options;
  Executor execute;
  BatchInputMeter meter;
  mutable std::mutex mutex;
  std::condition_variable changed;
  std::vector<std::shared_ptr<Job>> jobs;
  std::vector<std::thread> workers;
  std::thread recovery;
  std::atomic<bool> stopping{false}, failed{false};
  size_t cursor = 0, last_client = kMaxClients + 1, result_bytes = 0;
  uint64_t turns = 0, recovered = 0, interrupted = 0, errors = 0;
  uint64_t recovery_errors = 0, recovery_pending = 0;
  bool recovery_scanned = false;
  State(ObjectStore& o, ClientAdmission& c, ChatPlanConfig p,
        BatchJobsOptions limits, Executor e, BatchInputMeter measure)
      : objects(o),
        clients(c),
        config(std::move(p)),
        options(limits),
        execute(std::move(e)),
        meter(std::move(measure)),
        jobs(limits.max_jobs) {}
};

BatchJobs::BatchJobs(ObjectStore& objects, ClientAdmission& clients,
                     ChatPlanConfig config, BatchJobsOptions options,
                     Executor execute, BatchInputMeter meter) {
  if (!options.workers || options.workers > 8 || !options.max_jobs ||
      options.max_jobs > 64 || options.workers > options.max_jobs ||
      !options.max_client_jobs || options.max_client_jobs > options.max_jobs ||
      options.max_result_bytes < kChatBodyBytes ||
      options.max_result_bytes > (1ULL << 30) ||
      options.turn_timeout.count() < 1 ||
      options.turn_timeout > std::chrono::hours(24) ||
      options.retry_delay.count() < 1 ||
      options.retry_backoff < options.retry_delay ||
      options.scan_interval.count() < 1 || !execute)
    throw std::invalid_argument("Invalid native Batch executor limits");
  state_ = std::make_unique<State>(objects, clients, std::move(config), options,
                                   std::move(execute), std::move(meter));
  try {
    for (size_t i = 0; i < options.workers; ++i)
      state_->workers.emplace_back([this] { Worker(); });
    state_->recovery = std::thread([this] { Recover(); });
  } catch (...) {
    RequestStop();
    Join();
    throw;
  }
}
BatchJobs::~BatchJobs() {
  RequestStop();
  Join();
}

ObjectReply BatchJobs::Reserve(const std::shared_ptr<Job>& job) {
  const std::lock_guard lock(state_->mutex);
  if (state_->stopping || state_->failed) return Busy();
  size_t count = 0;
  std::shared_ptr<Job>* free = nullptr;
  for (auto& existing : state_->jobs) {
    if (!existing) {
      if (!free) free = &existing;
    } else if (existing->scope == job->scope) {
      ++count;
      if (existing->id == job->id)
        return Error(409, "batch_already_owned", "Batch already has an owner");
    }
  }
  if (!free || count >= state_->options.max_client_jobs)
    return Error(429, "batch_job_capacity", "Batch queue capacity exhausted");
  *free = job;  // shared_ptr assignment cannot allocate after reservation.
  return {};
}

ObjectReply BatchJobs::Submit(const BinaryAccess& access,
                              const BatchCreate& options,
                              const ClientIdentity& client) {
  const auto current = state_->clients.RecoveryIdentity(client.Index());
  if (!state_->clients.OwnsIdentity(client) || !current.Allowed() ||
      current.Id() != client.Id() || access.scope != Scope(current))
    return Error(403, "object_scope_mismatch", "Batch client changed");
  auto job = std::make_shared<Job>();
  job->id = NewObjectId(ObjectKind::kBatch);
  if (job->id.empty())
    return Error(503, "object_entropy_unavailable", "No Batch identity");
  job->scope = access.scope;
  job->client = current;
  job->scheduling_client = current.Index();
  auto accepted = Reserve(job);
  if (!accepted.ok()) return accepted;
  // Preserve this ID/slot even if constructing a return value throws after
  // storage has committed. No model is eligible until the worker reconciles.
  ScopeExit published([&] {
    {
      const std::lock_guard lock(state_->mutex);
      job->submitting = false;
    }
    state_->changed.notify_all();
  });
  accepted = BatchState(state_->objects).Create(access, options, job->id);
  if (!accepted.ok() && !accepted.commit_unknown) {
    const std::lock_guard lock(state_->mutex);
    for (auto& slot : state_->jobs)
      if (slot == job) slot.reset();
  }
  return accepted;
}

ObjectReply BatchJobs::Cancel(const BinaryAccess& access,
                              const std::string& id) {
  auto reply = BatchState(state_->objects).Cancel(access, id);
  if (reply.ok() || reply.commit_unknown) {
    const std::lock_guard lock(state_->mutex);
    for (const auto& job : state_->jobs) {
      if (!job || job->scope != access.scope || job->id != id) continue;
      job->cancel = true;
      job->next = Clock::now();
      if (job->control) job->control->Cancel();
    }
    state_->changed.notify_all();
  }
  return reply;
}

void BatchJobs::ReleaseResult(const std::shared_ptr<Job>& job) {
  job->result.reset();
  job->custom_id.clear();
  job->locally_drained.reset();
  job->local_ticket.clear();
  const std::lock_guard lock(state_->mutex);
  job->control.reset();
  if (job->result_reserved) {
    state_->result_bytes -= kChatBodyBytes;
    job->result_reserved = false;
  }
}

ObjectReply BatchJobs::SaveResult(const std::shared_ptr<Job>& job,
                                  const BinaryAccess& access) {
  auto saved = BatchState(state_->objects)
                   .FinishRow(access, job->id, job->result_index,
                              job->custom_id, *job->result);
  if (saved.ok()) {
    ReleaseResult(job);
  } else if (!saved.commit_unknown &&
             (saved.code == "batch_response_invalid" ||
              saved.code == "batch_result_capacity_exceeded")) {
    // The finite endpoint contract failed. Publish an honest per-row error
    // on the next turn instead of retrying the model or invalid JSON forever.
    job->result->response_body.clear();
    job->result->error = saved;
  }
  return saved;
}

ObjectReply BatchJobs::RunRow(const std::shared_ptr<Job>& job, const Json& data,
                              const BinaryAccess& access) {
  BatchState batches(state_->objects);
  const auto index = static_cast<size_t>(data.GetInt("next_row"));
  // A lost finish receipt is reconciled before another row can be considered.
  if (job->result) {
    if (index > job->result_index) {
      const auto committed = batches.Row(access, job->id, job->result_index);
      if (!committed.ok()) return committed;
      const auto status = committed.value.GetString("status");
      if ((status != "completed" && status != "failed") ||
          committed.value.GetString("request_id") != job->result->request_id)
        return Error(503, "batch_result_state_invalid",
                     "Stored result receipt is inconsistent");
      ReleaseResult(job);
      return {};
    }
    if (index != job->result_index)
      return Error(503, "batch_result_state_invalid",
                   "Stored result position is inconsistent");
    return SaveResult(job, access);
  }
  const auto& batch = *data.Find("batch");
  const auto status = batch.GetString("status");
  const auto total = batch.Find("request_counts")->GetInt("total");
  if (index >= static_cast<size_t>(total)) {
    ReleaseResult(job);
    if (status == "finalizing") return batches.Finalize(access, job->id);
    if (status == "cancelling")
      return batches.BeginFinalization(access, job->id, "cancelled");
    if (batch.GetInt("expires_at") <= std::time(nullptr))
      return batches.BeginFinalization(access, job->id, "expired");
    return batches.BeginFinalization(access, job->id, "completed");
  }
  auto checkpoint = batches.Row(access, job->id, index);
  if (!checkpoint.ok()) return checkpoint;
  const bool running = checkpoint.value.GetString("status") == "running";
  if (running) {
    const auto* owner = checkpoint.value.Find("execution_owner");
    const bool drained =
        job->locally_drained == index && !job->local_ticket.empty() &&
        checkpoint.value.GetString("request_id") == job->local_ticket;
    if (!drained &&
        (!owner || ResponseOwnerStatus(*owner) != ResponseOwnerState::kGone))
      return Error(503, "batch_execution_owner_unconfirmed",
                   "Started row owner is not confirmed drained");
  } else if (status == "cancelling") {
    return batches.BeginFinalization(access, job->id, "cancelled");
  } else if (status != "finalizing" &&
             batch.GetInt("expires_at") <= std::time(nullptr)) {
    return batches.BeginFinalization(access, job->id, "expired");
  }
  BatchInputRow row;
  auto read = batches.ReadInputRow(access, job->id, index, &row);
  if (!read.ok()) return read;
  if (running || status == "finalizing") {
    BatchRowResult result;
    result.request_id = running ? checkpoint.value.GetString("request_id") : "";
    const auto final = data.GetString("final_status");
    result.error = running ? Interrupted()
                   : final == "cancelled"
                       ? Error(499, "batch_cancelled", "Batch was cancelled")
                       : Error(408, "batch_expired", "Batch execution expired");
    auto saved =
        batches.FinishRow(access, job->id, index, row.custom_id, result);
    if (saved.ok()) {
      ReleaseResult(job);
      if (running) {
        const std::lock_guard lock(state_->mutex);
        ++state_->interrupted;
      }
    }
    return saved;
  }
  ClientLease permit;
  if (!permit.Acquire(state_->clients, job->client, ClientLane::kData))
    return Error(429, "batch_data_admission", "Client data capacity is busy");
  Json frozen;
  read = BatchPreparation(state_->objects)
             .Read(access, job->id, batch.GetString("endpoint"), row,
                   checkpoint.value, &frozen);
  if (!read.ok()) return read;
  {
    const std::lock_guard lock(state_->mutex);
    if (!job->result_reserved) {
      if (state_->result_bytes >
          state_->options.max_result_bytes - kChatBodyBytes)
        return Error(429, "batch_result_capacity",
                     "Batch pending result capacity is busy");
      state_->result_bytes += kChatBodyBytes;
      job->result_reserved = true;
    }
  }
  // Set before the mutation: an unknown StartRow can only be closed as an
  // interruption, never invoked. This thread owns it until callback drain.
  auto identity = NewObjectId(ObjectKind::kToolRun);
  if (identity.empty())
    return Error(503, "object_entropy_unavailable",
                 "No row execution identity");
  job->local_ticket = "req_" + identity;
  job->locally_drained = index;
  const auto started =
      batches.StartRow(access, job->id, index, job->local_ticket);
  if (!started.ok()) return started;
  const auto request_id = started.value.GetString("request_id");
  const auto remaining = batch.GetInt("expires_at") - std::time(nullptr);
  const auto deadline =
      std::min(access.deadline, Clock::now() + std::chrono::seconds(remaining));
  auto control = std::make_shared<RequestControl>(request_id, "", deadline,
                                                  job->client.Id());
  {
    const std::lock_guard lock(state_->mutex);
    job->control = control;
    if (state_->stopping) control->Cancel(RequestControl::State::kShutdown);
    if (job->cancel) control->Cancel();
  }
  job->custom_id = row.custom_id;
  job->result_index = index;
  job->result.emplace();
  job->result->request_id = request_id;
  job->result->error = Interrupted();
  // Even a throwing executor must have drained before returning control.
  try {
    if (control->Cancelled()) {
      job->result->error = Error(499, "batch_cancelled",
                                 "Execution was cancelled before invocation");
    } else {
      auto result = state_->execute(batch.GetString("endpoint"), row, frozen,
                                    job->client, permit, control);
      if (result.request_id != request_id ||
          result.response_body.size() > kChatBodyBytes) {
        job->result->error = Error(503, "batch_endpoint_result_invalid",
                                   "Native endpoint result is invalid");
      } else {
        *job->result = std::move(result);
      }
    }
  } catch (...) {
    // Keep the interruption cached; never replay the invocation.
  }
  // Cancellation of execution must not cancel storage of its drained result.
  const BinaryAccess close{
      job->scope, Clock::now() + std::chrono::seconds(2), {}};
  return SaveResult(job, close);
}

ObjectReply BatchJobs::Turn(const std::shared_ptr<Job>& job, bool* terminal) {
  BinaryAccess access{
      job->scope, Clock::now() + state_->options.turn_timeout, {}};
  BatchState batches(state_->objects);
  auto loaded = batches.Load(access, job->id);
  if (!loaded.ok()) {
    if (loaded.http_status == 404) *terminal = true;
    return loaded;
  }
  auto& data = *loaded.value.Find("data");
  const auto& batch = *data.Find("batch");
  auto status = batch.GetString("status");
  if (BatchTerminal(status)) {
    *terminal = true;
    return {};
  }
  const auto* owner = data.Find("owner");
  if (!owner || OutputJson(*owner) != OutputJson(CurrentResponseOwner())) {
    auto claimed = batches.ClaimAbandoned(access, job->id);
    if (claimed.ok()) {
      const std::lock_guard lock(state_->mutex);
      ++state_->recovered;
    }
    return claimed;  // Reload the actual owner/state on the next fair turn.
  }
  const auto current = state_->clients.RecoveryIdentity(job->client.Index());
  const bool authorized = current.Allowed() && Scope(current) == job->scope;
  if (authorized) job->client = current;
  if ((!authorized || job->cancel) && status != "cancelling" &&
      status != "finalizing") {
    if (!authorized && status == "validating")
      return batches.FailValidation(access, job->id,
                                    Error(403, "batch_client_revoked",
                                          "Original client is unauthorized"));
    return batches.Cancel(access, job->id);
  }
  if (status == "validating") {
    if (batch.GetInt("expires_at") <= std::time(nullptr))
      return batches.BeginFinalization(access, job->id, "expired");
    if (!job->validation_error.ok())
      return batches.FailValidation(access, job->id, job->validation_error);
    if (!job->validation) {
      job->validation_client = job->client;
      auto config = state_->config;
      config.client_limits = job->validation_client.Limits();
      job->validation = std::make_unique<BatchValidation>(
          state_->objects, job->id, config, state_->meter);
    }
    access.cancelled = [&] { return state_->stopping.load() || job->cancel; };
    auto result = job->validation->Step(access);
    if (!result.ok()) {
      job->validation.reset();
      // A failed read/publication is not a malformed user's JSONL.
      if (!result.commit_unknown && result.http_status >= 400 &&
          result.http_status < 500 && result.http_status != 408 &&
          result.http_status != 409 && result.http_status != 429 &&
          result.http_status != 499)
        job->validation_error = result;
    }
    return result;
  }
  job->validation.reset();
  return RunRow(job, data, access);
}

void BatchJobs::Worker() noexcept {
  try {
    for (;;) {
      std::shared_ptr<Job> job;
      {
        std::unique_lock lock(state_->mutex);
        while (!state_->stopping) {
          const auto now = Clock::now();
          // Rotate both client identity and job slot. Multiple jobs owned by
          // one client cannot consume every turn while another is ready.
          for (int pass = 0; pass < 2 && !job; ++pass) {
            for (size_t n = 0; n < state_->jobs.size(); ++n) {
              const size_t i = (state_->cursor + n) % state_->jobs.size();
              const auto& candidate = state_->jobs[i];
              if (!candidate || candidate->submitting || candidate->active ||
                  candidate->next > now ||
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
        job->validation.reset();
      }
      if (terminal) ReleaseResult(job);
      {
        const std::lock_guard lock(state_->mutex);
        ++state_->turns;
        job->active = false;
        if (terminal) {
          for (auto& slot : state_->jobs)
            if (slot == job) slot.reset();
        } else if (!result.ok()) {
          ++state_->errors;
          ++job->attempts;
          job->next = Clock::now() + (job->attempts % 3 == 0
                                          ? state_->options.retry_backoff
                                          : state_->options.retry_delay);
        } else {
          job->attempts = 0;
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

void BatchJobs::Recover() noexcept {
  try {
    std::string cursor;
    size_t pending = 0, errors = 0;
    while (!state_->stopping) {
      const auto cancelled = [&] { return state_->stopping.load(); };
      try {
        auto page = state_->objects.ScanBatches(
            cursor, Clock::now() + std::chrono::seconds(2), cancelled);
        if (page.ok()) {
          const auto* rows = page.value.GetArray("data");
          if (!rows) throw std::runtime_error("Invalid Batch recovery page");
          for (const auto& row : rows->array) {
            if (state_->stopping) break;
            const auto scope = row.GetString("scope"), id = row.GetString("id");
            bool owned = false;
            {
              const std::lock_guard lock(state_->mutex);
              for (const auto& job : state_->jobs) {
                if (!job || job->scope != scope || job->id != id) continue;
                owned = true;
                if (row.GetString("status") == "cancelling") {
                  job->cancel = true;
                  if (job->control) job->control->Cancel();
                }
                break;
              }
            }
            if (owned) continue;
            const auto* owner = row.Find("owner");
            if (scope.empty() || !owner ||
                ResponseOwnerStatus(*owner) != ResponseOwnerState::kGone) {
              ++pending;
              continue;
            }
            auto job = std::make_shared<Job>();
            job->scope = scope;
            job->id = id;
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
            // Revoked clients still need an owner to close their durable jobs.
            if (Reserve(job).ok()) {
              const std::lock_guard lock(state_->mutex);
              job->submitting = false;
              state_->changed.notify_all();
            } else {
              ++pending;
            }
          }
          cursor = page.value.GetString("cursor");
          if (!page.value.GetBool("has_more")) {
            cursor.clear();
            const std::lock_guard lock(state_->mutex);
            state_->recovery_scanned = true;
            state_->recovery_pending = pending;
            pending = 0;
          }
          errors = 0;
        } else {
          ++errors;
          const std::lock_guard lock(state_->mutex);
          ++state_->recovery_errors;
        }
      } catch (...) {
        // One failed scan cannot permanently disable recovery or stop owned
        // work. Keep its cursor, use the same bounded worker and backoff.
        ++errors;
        const std::lock_guard lock(state_->mutex);
        ++state_->recovery_errors;
      }
      std::unique_lock lock(state_->mutex);
      const auto delay = errors && errors % 3 == 0
                             ? state_->options.retry_backoff
                             : state_->options.scan_interval;
      state_->changed.wait_for(lock, delay, cancelled);
    }
  } catch (...) {
    // Scanner failure does not stop healthy model execution or owned jobs.
    const std::lock_guard lock(state_->mutex);
    ++state_->recovery_errors;
  }
}

void BatchJobs::RequestStop() noexcept {
  if (!state_) return;
  state_->stopping = true;
  const std::lock_guard lock(state_->mutex);
  for (const auto& job : state_->jobs)
    if (job && job->control)
      job->control->Cancel(RequestControl::State::kShutdown);
  state_->changed.notify_all();
}
void BatchJobs::Join() {
  if (state_->recovery.joinable()) state_->recovery.join();
  for (auto& worker : state_->workers)
    if (worker.joinable()) worker.join();
}
BatchJobsHealth BatchJobs::Health() const {
  const std::lock_guard lock(state_->mutex);
  BatchJobsHealth health;
  for (const auto& job : state_->jobs) {
    if (!job) continue;
    ++health.owned;
    health.active += job->active;
  }
  health.reserved_result_bytes = state_->result_bytes;
  health.turns = state_->turns;
  health.recovered = state_->recovered;
  health.interrupted = state_->interrupted;
  health.errors = state_->errors;
  health.recovery_errors = state_->recovery_errors;
  health.recovery_pending = state_->recovery_pending;
  health.stopping = state_->stopping;
  health.failed = state_->failed;
  health.recovery_scanned = state_->recovery_scanned;
  return health;
}
}  // namespace q4t::server
