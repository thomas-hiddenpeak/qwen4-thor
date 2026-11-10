#include "q4t/server/responses_jobs.h"

#include <algorithm>
#include <atomic>
#include <array>
#include <condition_variable>
#include <ctime>
#include <cstring>
#include <deque>
#include <mutex>
#include <optional>
#include <thread>
#include <unordered_map>

#include "object_protocol.h"
#include "q4t/server/lifetime.h"
#include "q4t/server/responses_owner.h"

namespace q4t::server {
namespace {
using namespace object_detail;
using Clock = std::chrono::steady_clock;
bool Terminal(const Json& response) {
  const auto status = response.GetString("status");
  return status == "completed" || status == "incomplete" ||
         status == "failed" || status == "cancelled";
}
std::string Key(const std::string& scope, const std::string& id) {
  return scope + '\0' + id;
}
}  // namespace

struct ResponsesJobs::State {
  struct Foreground {
    uint64_t ticket = 0;
    std::array<char, 257> scope{};
    std::array<char, 129> id{};
    bool active = true;
    bool closing = false;
    size_t attempts = 0;
    Clock::time_point next;
  };
  ObjectStore& objects;
  ResponsesJobsOptions options;
  Executor executor;
  RateAdmission rate_admission;
  mutable std::mutex mutex;
  std::condition_variable changed;
  std::unordered_map<std::string, std::shared_ptr<Job>> jobs;
  std::deque<std::shared_ptr<Job>> queue;
  std::vector<std::thread> workers;
  std::thread recovery;
  bool recovery_configured = false;
  std::atomic<bool> recovery_scanned{false};
  std::atomic<uint64_t> recovered{0}, interrupted{0}, recovery_errors{0};
  std::atomic<size_t> recovery_pending{0};
  size_t last_client = kMaxClients;
  size_t bytes = 0;
  std::atomic<bool> stopping{false};
  std::atomic<bool> failed{false};
  std::vector<Foreground> foreground;
  uint64_t next_ticket = 0;
  uint64_t foreground_reconciled = 0;
  uint64_t foreground_errors = 0;
  bool last_foreground = false;
  uint64_t rate_deferrals = 0;
  State(ObjectStore& store, ResponsesJobsOptions limits, Executor run,
        RateAdmission admit)
      : objects(store),
        options(limits),
        executor(std::move(run)),
        rate_admission(std::move(admit)),
        foreground(std::min<size_t>(256, limits.max_jobs)) {}
  bool ReadyQueue() const {
    return std::any_of(queue.begin(), queue.end(), [&](const auto& job) {
      return stopping || job->control->Cancelled() ||
             Clock::now() >= job->next_rate;
    });
  }
  size_t ReadyForeground() const {
    const auto now = Clock::now();
    for (size_t i = 0; i < foreground.size(); ++i) {
      const auto& item = foreground[i];
      if (item.ticket && !item.active && !item.closing && now >= item.next)
        return i;
    }
    return foreground.size();
  }
};

ResponsesJobs::ResponsesJobs(ObjectStore& objects, ResponsesJobsOptions options,
                             Executor executor, RateAdmission rate_admission)
    : state_(std::make_unique<State>(objects, options, std::move(executor),
                                     std::move(rate_admission))) {
  if (!options.workers || options.workers > 32 || !options.max_jobs ||
      options.max_jobs > 256 || options.workers > options.max_jobs ||
      !options.max_client_jobs || options.max_client_jobs > options.max_jobs ||
      !options.max_input_bytes ||
      options.max_input_bytes > kMaxBackgroundInputBytes ||
      options.timeout.count() < 1 || options.timeout > std::chrono::hours(24) ||
      !state_->executor)
    throw std::invalid_argument("Invalid background execution limits");
  try {
    for (size_t i = 0; i < options.workers; ++i)
      state_->workers.emplace_back([this] { Worker(); });
  } catch (...) {
    RequestStop();
    Join();
    throw;
  }
}

ResponsesJobs::~ResponsesJobs() {
  RequestStop();
  Join();
}

ObjectReply ResponsesJobs::Reserve(const std::string& scope,
                                   const std::string& id, uint64_t* ticket) {
  if (!ticket || *ticket || scope.empty() || scope.size() > 256 || id.empty() ||
      id.size() > 128)
    return Error(500, "response_reconciliation_invalid",
                 "Invalid lifecycle owner");
  const std::lock_guard lock(state_->mutex);
  if (state_->stopping || state_->failed)
    return Error(503, "response_reconciliation_unavailable",
                 "Lifecycle owner stopping");
  size_t client_count = 0;
  State::Foreground* available = nullptr;
  for (auto& item : state_->foreground) {
    if (!item.ticket) {
      if (!available) available = &item;
    } else if (scope == item.scope.data()) {
      ++client_count;
      if (id == item.id.data())
        return Error(409, "response_already_owned",
                     "Response already has a lifecycle owner");
    }
  }
  if (!available || client_count >= state_->options.max_client_jobs)
    return Error(429, "response_reconciliation_capacity",
                 "Lifecycle metadata capacity exhausted");
  *available = {};
  std::memcpy(available->scope.data(), scope.data(), scope.size());
  std::memcpy(available->id.data(), id.data(), id.size());
  if (++state_->next_ticket == 0) ++state_->next_ticket;
  available->ticket = *ticket = state_->next_ticket;
  return {};
}

void ResponsesJobs::Close(uint64_t ticket, bool known_terminal) noexcept {
  {
    const std::lock_guard lock(state_->mutex);
    for (auto& item : state_->foreground) {
      if (item.ticket != ticket || !ticket || !item.active) continue;
      if (known_terminal)
        item = {};
      else {
        item.active = false;
        item.next = Clock::now();
      }
      break;
    }
  }
  state_->changed.notify_all();
}

void ResponsesJobs::ReconcileForeground(uint64_t ticket, std::string_view scope,
                                        std::string_view id) noexcept {
  bool terminal = false;
  try {
    StateAccess access{
        std::string(scope), Clock::now() + std::chrono::seconds(2), {}};
    const auto reply = ResponsesState(state_->objects)
                           .Interrupt(std::string(id), access,
                                      "response_execution_interrupted",
                                      "Execution owner completed cleanup "
                                      "without a confirmed terminal result");
    terminal = reply.ok() || reply.http_status == 404;
  } catch (...) {
    // Preserve the slot and retry budget even when constructing a read fails.
  }
  const std::lock_guard lock(state_->mutex);
  for (auto& item : state_->foreground) {
    if (item.ticket != ticket || !item.closing) continue;
    if (terminal) {
      item = {};
      ++state_->foreground_reconciled;
    } else {
      ++state_->foreground_errors;
      ++item.attempts;
      item.closing = false;
      item.next = Clock::now() + (item.attempts % 3 == 0
                                      ? std::chrono::milliseconds(60000)
                                      : std::chrono::milliseconds(250));
    }
    break;
  }
}

ObjectReply ResponsesJobs::Submit(std::unique_ptr<ResponsesExecution> run,
                                  ClientIdentity client,
                                  const TransferPermit& transfer,
                                  std::shared_ptr<ClientRateLease> rate) {
  return Adopt(std::move(run), client, transfer, false, std::move(rate));
}

ObjectReply ResponsesJobs::Adopt(std::unique_ptr<ResponsesExecution> run,
                                 ClientIdentity client,
                                 const TransferPermit& transfer, bool restored,
                                 std::shared_ptr<ClientRateLease> rate) {
  ScopeExit settle_rate([&] {
    if (rate) rate->Settle();
  });
  if (!run || !run->Prepared() || run->Begun() != restored ||
      !run->Envelope().background || !client.Allowed() || !transfer)
    return Error(500, "background_state_invalid", "Invalid background handoff");
  const auto scope = client.Scoped() ? "client:" + std::string(client.Id())
                                     : "local:anonymous";
  if (scope != run->Scope())
    return Error(403, "object_scope_mismatch", "Background client changed");
  if (rate && (restored || run->RateDomain() != ClientRateDomain::kBatch ||
               !rate->PrechargedFor(client)))
    return Error(500, "background_rate_invalid",
                 "Background rate ownership is invalid");
  auto job = std::make_shared<Job>();
  job->bytes = run->RetainedInputBytes();
  auto duration = state_->options.timeout;
  if (!restored && run->RateDomain() == ClientRateDomain::kBatch) {
    // A detached Batch background job retains the original row/24h bound.
    // Ordinary online background jobs still outlive their HTTP subscriber.
    const auto remaining =
        std::chrono::duration_cast<std::chrono::milliseconds>(
            run->InputDeadline() - Clock::now());
    if (remaining.count() <= 0)
      return Error(408, "deadline_exceeded",
                   "Original Batch execution deadline expired");
    duration = std::min(duration, remaining);
  }
  if (restored) {
    const auto remaining = run->ExecutionDeadlineAt() - std::time(nullptr);
    if (remaining <= 0)
      return Error(408, "deadline_exceeded",
                   "Queued response deadline expired");
    duration = std::min(duration,
                        std::chrono::duration_cast<std::chrono::milliseconds>(
                            std::chrono::seconds(remaining)));
  }
  job->control = std::make_shared<RequestControl>(
      run->Id(), "", Clock::now() + duration, client.Id());
  job->client = client;
  job->run = std::move(run);
  job->run->BindBackgroundControl(job->control);
  const auto key = Key(scope, job->run->Id());
  ObjectReply accepted;
  accepted.value = job->run->Output().Snapshot();
  {
    const std::lock_guard lock(state_->mutex);
    if (state_->stopping || state_->failed)
      return Error(503, "background_unavailable",
                   "Background executor is stopping");
    if (state_->jobs.contains(key))
      return Error(409, "background_already_owned",
                   "Response already has an owner");
    size_t client_count = 0;
    for (const auto& [_, existing] : state_->jobs)
      client_count += existing->run->Scope() == scope;
    if (state_->jobs.size() >= state_->options.max_jobs ||
        client_count >= state_->options.max_client_jobs ||
        job->bytes > state_->options.max_input_bytes ||
        state_->bytes > state_->options.max_input_bytes - job->bytes)
      return Error(429, "background_capacity_exhausted",
                   "Background queue is full");
    state_->jobs.emplace(key, job);
    state_->bytes += job->bytes;
  }
  bool owned = false;
  ScopeExit rollback([&] {
    if (!owned) {
      try {
        job->run->CloseUnfinished();
      } catch (...) {
      }
      try {
        Release(job, job->run->Finished() ||
                         (!job->run->Begun() && !job->run->CommitUnknown()));
      } catch (...) {
        state_->failed.store(true);
      }
    }
  });
  job->rate = std::move(rate);
  std::string ignored;
  auto result = restored ? ObjectReply() : job->run->Begin(&ignored);
  if (!result.ok()) return result;
  if (!transfer(*job->permit))
    return Error(503, "background_admission_lost", "Client permit unavailable");
  bool close_on_submit = false;
  {
    const std::lock_guard lock(state_->mutex);
    if (state_->stopping) {
      job->control->Cancel(RequestControl::State::kShutdown);
      job->stage = Job::Stage::kClosing;
      close_on_submit = true;
    } else {
      state_->queue.push_back(job);
      job->stage = Job::Stage::kQueued;
    }
    owned = true;
  }
  if (close_on_submit) {
    Execute(job);
    return ResponsesState(state_->objects)
        .Retrieve(job->run->Id(),
                  {scope, Clock::now() + std::chrono::seconds(2), {}});
  }
  state_->changed.notify_all();
  return accepted;
}

ObjectReply AdmitBackgroundBatchRate(ClientAdmission& clients,
                                     ResponsesJobs::Job& job,
                                     ChatPlanConfig config,
                                     const BackgroundRatePlanner& planner) {
  const auto error = [](int status, std::string code, std::string message) {
    ObjectReply reply;
    reply.http_status = status;
    reply.code = std::move(code);
    reply.message = std::move(message);
    return reply;
  };
  if (!job.run || !job.control || !planner || job.rate ||
      !job.run->Prepared() || !job.run->Begun() || job.run->Finished() ||
      job.run->CommitUnknown() ||
      job.run->RateDomain() != ClientRateDomain::kBatch ||
      !clients.OwnsIdentity(job.client))
    return error(500, "background_rate_invalid",
                 "Background rate admission owner is invalid");
  const auto client = clients.RecoveryIdentity(job.client.Index());
  if (!client.Allowed() || client.Id() != job.client.Id() ||
      job.run->Scope() != (client.Scoped()
                               ? "client:" + std::string(client.Id())
                               : "local:anonymous"))
    return error(403, "background_client_revoked",
                 "Original background client is unauthorized");
  job.client = client;
  config.client_limits = job.client.Limits();
  if (!job.planned_rate || job.planned_client.Limits() != job.client.Limits()) {
    BatchRateBudget budget;
    const auto result =
        planner(*job.run, config,
                {job.run->Scope(), job.control->deadline(),
                 [control = job.control] { return control->Cancelled(); }},
                &budget);
    if (!result.ok()) return result;
    if (!budget.prompt_capacity || budget.prompt_capacity > kBatchTokenLimit ||
        budget.output > kBatchTokenLimit - budget.prompt_capacity ||
        budget.auxiliary_output > kMaxAuxiliaryOutputTokens ||
        budget.preparation_output > kMaxPreparationOutputTokens ||
        budget.Total() > kBatchTokenLimit)
      return error(500, "background_rate_budget_invalid",
                   "Background execution budget is invalid");
    job.planned_rate = budget;
    job.planned_client = job.client;
  }
  const auto& budget = *job.planned_rate;
  if (config.client_limits && config.client_limits->tokens_per_minute &&
      budget.Total() > config.client_limits->tokens_per_minute)
    return error(
        429, "batch_request_token_reservation_exceeded",
        budget.input_exact
            ? "Request input/output reservation exceeds TPM"
            : "Multi-step input ceiling/output reservation exceeds TPM");
  if (clients.RatesFrozen())
    return error(503, "batch_rate_wait", "Batch rate admission is draining");
  if (config.client_limits) {
    const auto usage = clients.RateUsage(job.client.Index(), ClientClock::now(),
                                         ClientRateDomain::kBatch);
    const auto& limits = *config.client_limits;
    const uint64_t ceiling = limits.tokens_per_minute;
    if ((limits.requests_per_minute &&
         usage.requests >= limits.requests_per_minute) ||
        (ceiling &&
         (usage.tokens > ceiling || usage.reserved > ceiling - usage.tokens ||
          budget.Total() > ceiling - usage.tokens - usage.reserved)))
      return error(429, "batch_rate_wait",
                   "Background task awaits original Batch minute quota");
  }
  if (!job.permit || !job.permit->Holds(job.client, ClientLane::kData)) {
    job.permit.emplace();
    if (!job.permit->Acquire(clients, job.client, ClientLane::kData))
      return error(429, "batch_data_wait",
                   "Background task awaits client data admission");
  }
  auto rate = std::make_shared<ClientRateLease>(ClientRateDomain::kBatch);
  const auto admitted = rate->PrechargeBatch(
      clients, job.client, budget.prompt_capacity, budget.output,
      ClientClock::now(), budget.auxiliary_output, budget.preparation_output);
  if (admitted != ClientRateResult::kAllowed)
    return error(admitted == ClientRateResult::kFrozen ? 503 : 429,
                 admitted == ClientRateResult::kInvalid
                     ? "background_rate_prepare_retry"
                     : "batch_rate_wait",
                 "Background Batch rate admission is unavailable");
  job.rate = std::move(rate);
  return {};
}

void ResponsesJobs::StartRecovery(ClientAdmission& clients,
                                  ChatPlanConfig config) {
  const std::lock_guard lock(state_->mutex);
  if (state_->recovery_configured || state_->stopping)
    throw std::logic_error("Response recovery already configured or stopping");
  state_->recovery =
      std::thread([this, &clients, config = std::move(config)]() mutable {
        Recover(clients, std::move(config));
      });
  state_->recovery_configured = true;
}

void ResponsesJobs::Recover(ClientAdmission& clients,
                            ChatPlanConfig config) noexcept {
  while (!state_->stopping.load()) {
    try {
      std::unordered_map<std::string, ClientIdentity> identities;
      std::unordered_map<std::string, std::string> scopes;
      scopes.emplace(Digest("local:anonymous"), "local:anonymous");
      for (size_t i = 0; i < std::max<size_t>(1, clients.Size()); ++i) {
        const auto identity = clients.RecoveryIdentity(i);
        if (!identity.Allowed()) continue;
        const auto scope = identity.Scoped()
                               ? "client:" + std::string(identity.Id())
                               : "local:anonymous";
        identities.emplace(scope, identity);
        scopes.emplace(Digest(scope), scope);
      }
      std::string cursor;
      size_t failures = 0, pending = 0;
      const auto cancelled = [&] { return state_->stopping.load(); };
      const auto wait = [&](std::chrono::milliseconds delay) {
        std::unique_lock lock(state_->mutex);
        state_->changed.wait_for(lock, delay, cancelled);
      };
      while (!cancelled()) {
        const auto page = state_->objects.ScanResponses(
            cursor, Clock::now() + std::chrono::seconds(2), cancelled);
        if (!page.ok()) {
          ++state_->recovery_errors;
          ++failures;
          wait(failures % 3 == 0 ? std::chrono::milliseconds(60000)
                                 : std::chrono::milliseconds(250));
          continue;
        }
        failures = 0;
        const auto* rows = page.value.GetArray("data");
        if (!rows) throw std::runtime_error("Invalid response recovery page");
        for (const auto& row : rows->array) {
          if (cancelled()) break;
          auto scope = row.GetString("scope");
          if (scope.empty()) {
            const auto known = scopes.find(row.GetString("owner_digest"));
            if (known != scopes.end()) scope = known->second;
          }
          const auto id = row.GetString("id");
          {
            const std::lock_guard lock(state_->mutex);
            if (state_->jobs.contains(Key(scope, id))) continue;
          }
          const auto* owner = row.Find("owner");
          const auto status = owner ? ResponseOwnerStatus(*owner)
                                    : ResponseOwnerState::kUnknown;
          // Includes current foreground execution and other live processes.
          // A dead storage worker alone never grants replay authority.
          if (scope.empty() || status != ResponseOwnerState::kGone) {
            ++pending;
            continue;
          }
          StateAccess access{scope, Clock::now() + std::chrono::seconds(2),
                             cancelled};
          const auto identity = identities.find(scope);
          const auto close = [&](std::string_view code,
                                 std::string_view message) {
            const auto result = ResponsesState(state_->objects)
                                    .Interrupt(id, access, code, message);
            if (result.ok() || result.http_status == 404)
              ++state_->interrupted;
            else {
              ++pending;
              ++state_->recovery_errors;
            }
          };
          if (identity == identities.end()) {
            close("background_client_revoked",
                  "Original client is no longer authorized");
            continue;
          }
          if (!row.GetBool("background") ||
              row.GetString("status") != "queued") {
            close("background_execution_interrupted",
                  "Previous execution process exited; started work is not "
                  "replayed");
            continue;
          }
          if (row.GetInt("execution_deadline_at") <= std::time(nullptr)) {
            close("deadline_exceeded",
                  "Queued response execution deadline expired");
            continue;
          }
          ClientLease permit;
          if (!permit.Acquire(clients, identity->second, ClientLane::kData)) {
            ++pending;
            continue;
          }
          auto run = std::make_unique<ResponsesExecution>(state_->objects);
          config.client_limits = identity->second.Limits();
          auto restored = run->RestoreQueued(id, config, access);
          if (!restored.ok()) {
            if (restored.http_status >= 500) {
              ++pending;
              ++state_->recovery_errors;
            } else
              close("background_restore_rejected", restored.message);
            continue;
          }
          restored = Adopt(
              std::move(run), identity->second,
              [&](ClientLease& target) { return permit.TransferTo(target); },
              true);
          if (restored.ok())
            ++state_->recovered;
          else if (restored.code == "background_already_owned")
            continue;
          else if (restored.http_status == 408)
            close(restored.code, restored.message);
          else {
            ++pending;
            ++state_->recovery_errors;
          }
        }
        cursor = page.value.GetString("cursor");
        if (!page.value.GetBool("has_more")) {
          state_->recovery_pending = pending;
          state_->recovery_scanned = true;
          // Keep observing foreign live/unknown owners and capacity deferrals.
          // No launches or model restarts are hidden in the recovery loop.
          if (!pending) return;
          pending = 0;
          cursor.clear();
          wait(std::chrono::milliseconds(1000));
        } else
          wait(std::chrono::milliseconds(25));
      }
    } catch (...) {
      ++state_->recovery_errors;
      // Scanner failure is visible and never stops the healthy model or jobs.
      state_->recovery_pending.fetch_add(1);
      try {
        std::unique_lock lock(state_->mutex);
        state_->changed.wait_for(lock, std::chrono::seconds(60),
                                 [&] { return state_->stopping.load(); });
      } catch (...) {
        return;  // Never escape the auxiliary thread into std::terminate.
      }
    }
  }
}

void ResponsesJobs::Release(const std::shared_ptr<Job>& job,
                            bool known_terminal) {
  // Execution has drained even when its terminal storage receipt is unknown.
  // Reconciliation retains metadata, never a live token reservation.
  if (auto rate = std::move(job->rate)) rate->Settle();
  const std::lock_guard lock(state_->mutex);
  job->rate_waiting = false;
  if (!known_terminal) {
    job->stage = Job::Stage::kUncertain;
    job->next_recovery = Clock::now() + (job->recovery_attempts &&
                                                 job->recovery_attempts % 3 == 0
                                             ? std::chrono::milliseconds(60000)
                                             : std::chrono::milliseconds(250));
  } else {
    const auto found =
        std::find_if(state_->jobs.begin(), state_->jobs.end(),
                     [&](const auto& entry) { return entry.second == job; });
    if (found != state_->jobs.end() && found->second == job) {
      state_->bytes -= job->bytes;
      state_->jobs.erase(found);
    }
  }
  state_->changed.notify_all();
}

void ResponsesJobs::Reconcile(const std::shared_ptr<Job>& job) noexcept {
  try {
    ++job->recovery_attempts;
    StateAccess access{
        job->run->Scope(), Clock::now() + std::chrono::milliseconds(2000), {}};
    // No executor owns this job now. Read a fresh revision before deciding;
    // an already committed success wins and must never become failed.
    const auto result =
        ResponsesState(state_->objects)
            .Interrupt(
                job->run->Id(), access, "background_execution_interrupted",
                "Execution owner ended without a confirmed terminal result");
    Release(job, result.ok() || result.http_status == 404);
  } catch (...) {
    try {
      Release(job, false);
    } catch (...) {
      state_->failed.store(true);
    }
  }
}

void ResponsesJobs::Execute(const std::shared_ptr<Job>& job) noexcept {
  try {
    ObjectReply admission;
    if (!job->control->Cancelled() &&
        job->run->RateDomain() == ClientRateDomain::kBatch && !job->rate) {
      admission = state_->rate_admission
                      ? state_->rate_admission(*job)
                      : Error(503, "background_rate_prepare_retry",
                              "Batch background rate admission unavailable");
      const bool waiting = admission.code == "batch_rate_wait" ||
                           admission.code == "batch_data_wait" ||
                           admission.code == "background_rate_prepare_retry";
      if (waiting) {
        // No model callback or StartRunning occurred. Release the temporary
        // data permit as well, so a minute wait cannot block online traffic.
        if (auto rate = std::move(job->rate)) rate->Settle();
        job->permit.reset();
        const std::lock_guard lock(state_->mutex);
        if (!state_->stopping && !job->control->Cancelled()) {
          state_->queue.push_back(job);
          job->stage = Job::Stage::kQueued;
          job->rate_waiting = true;
          job->next_rate = Clock::now() + std::chrono::milliseconds(250);
          ++state_->rate_deferrals;
          state_->changed.notify_all();
          return;
        }
        if (state_->stopping)
          job->control->Cancel(RequestControl::State::kShutdown);
      }
      if (admission.ok() &&
          (!job->rate || !job->rate->PrechargedFor(job->client) ||
           !job->permit || !job->permit->Holds(job->client, ClientLane::kData)))
        admission = Error(500, "background_rate_invalid",
                          "Background rate admission has no valid owner");
      if (!admission.ok() && !job->control->Cancelled())
        ResponsesExecution::ObserveError(job->run.get(), admission.http_status,
                                         admission.message, admission.code);
    }
    if (admission.ok() && !job->control->Cancelled()) {
      std::string ignored;
      const auto started = job->run->StartRunning(&ignored);
      if (started.ok()) {
        state_->executor(*job);
      } else {
        ResponsesExecution::ObserveError(job->run.get(), started.http_status,
                                         started.message, started.code);
      }
    }
  } catch (const std::bad_alloc&) {
    ResponsesExecution::ObserveError(job->run.get(), 503,
                                     "Background memory allocation failed",
                                     "host_memory_exhausted");
  } catch (...) {
    ResponsesExecution::ObserveError(job->run.get(), 500,
                                     "Background executor failed",
                                     "background_execution_failed");
  }
  try {
    if (job->control->Cancelled() &&
        (std::string_view(job->control->Reason()) == "explicit_cancel" ||
         std::string_view(job->control->Reason()) == "server_shutdown")) {
      job->run->Cancel();
    } else {
      if (job->control->Cancelled())
        ResponsesExecution::ObserveError(job->run.get(), 408,
                                         "Background deadline exceeded",
                                         "deadline_exceeded");
      job->run->CloseUnfinished();
    }
    Release(job, job->run->Finished());
  } catch (...) {
    // Keep ownership for state reconciliation, never restart a healthy model.
    try {
      Release(job, false);
    } catch (...) {
      state_->failed.store(true);
    }
  }
}

void ResponsesJobs::Worker() noexcept {
  try {
    for (;;) {
      std::shared_ptr<Job> job;
      std::optional<State::Foreground> foreground;
      bool reconcile = false;
      {
        std::unique_lock lock(state_->mutex);
        state_->changed.wait_for(lock, std::chrono::milliseconds(250), [&] {
          return state_->stopping || state_->ReadyQueue() ||
                 state_->ReadyForeground() < state_->foreground.size();
        });
        if (state_->queue.empty() && state_->stopping) return;
        const auto next_foreground = state_->ReadyForeground();
        const auto take_foreground = [&] {
          auto& item = state_->foreground[next_foreground];
          item.closing = true;
          foreground = item;
        };
        if (next_foreground < state_->foreground.size() &&
            !state_->last_foreground)
          take_foreground();
        for (const auto& [_, candidate] : state_->jobs) {
          if (foreground) break;
          if (candidate->stage == Job::Stage::kUncertain &&
              Clock::now() >= candidate->next_recovery) {
            job = candidate;
            job->stage = Job::Stage::kClosing;
            reconcile = true;
            break;
          }
        }
        if (!job && !foreground && !state_->ReadyQueue() &&
            next_foreground < state_->foreground.size())
          take_foreground();
        if (!job && !foreground && !state_->ReadyQueue()) continue;
        if (!job && !foreground) {
          const auto rank = [&](const auto& candidate) {
            return (candidate->client.Index() + kMaxClients -
                    state_->last_client) %
                   (kMaxClients + 1);
          };
          auto choice = state_->queue.end();
          for (auto it = state_->queue.begin(); it != state_->queue.end();
               ++it) {
            if (!state_->stopping && !(*it)->control->Cancelled() &&
                Clock::now() < (*it)->next_rate)
              continue;
            if (choice == state_->queue.end() || rank(*it) < rank(*choice))
              choice = it;
          }
          if (choice == state_->queue.end()) continue;
          job = *choice;
          state_->queue.erase(choice);
          state_->last_client = job->client.Index();
          job->stage = Job::Stage::kRunning;
          job->rate_waiting = false;
          if (state_->stopping)
            job->control->Cancel(RequestControl::State::kShutdown);
        }
        state_->last_foreground = foreground.has_value();
      }
      if (foreground)
        ReconcileForeground(foreground->ticket, foreground->scope.data(),
                            foreground->id.data());
      else if (reconcile)
        Reconcile(job);
      else
        Execute(job);
    }
  } catch (...) {
    state_->failed.store(true);
    state_->stopping.store(true);
    state_->changed.notify_all();
  }
}

ObjectReply ResponsesJobs::Cancel(const std::string& id,
                                  const StateAccess& access) {
  ResponsesState states(state_->objects);
  auto current = states.Retrieve(id, access);
  if (!current.ok()) return current;
  if (!current.value.GetBool("background"))
    return Error(400, "response_not_background",
                 "Only background responses can be cancelled");
  if (Terminal(current.value)) return current;
  std::shared_ptr<Job> queued;
  {
    const std::lock_guard lock(state_->mutex);
    const auto found = state_->jobs.find(Key(access.scope, id));
    if (found == state_->jobs.end())
      return Error(503, "response_recovery_pending",
                   "Response awaits owner recovery");
    const auto& job = found->second;
    job->control->Cancel();
    if (job->stage == Job::Stage::kQueued) {
      queued = job;
      std::erase(state_->queue, job);
      job->stage = Job::Stage::kClosing;
    }
  }
  if (queued) Execute(queued);
  while (Clock::now() < access.deadline) {
    if (access.cancelled && access.cancelled())
      return Error(408, "response_cancel_wait_ended",
                   "Cancellation remains requested; retrieve the response");
    current = states.Retrieve(id, access);
    if (!current.ok() || Terminal(current.value)) return current;
    std::unique_lock lock(state_->mutex);
    state_->changed.wait_for(lock, std::chrono::milliseconds(20));
  }
  return Error(503, "response_cancel_pending",
               "Cancellation remains requested; retrieve the response");
}

void ResponsesJobs::RequestStop() noexcept {
  state_->stopping.store(true);
  try {
    const std::lock_guard lock(state_->mutex);
    for (const auto& [_, job] : state_->jobs)
      job->control->Cancel(RequestControl::State::kShutdown);
  } catch (...) {
    state_->failed.store(true);
  }
  state_->changed.notify_all();
}

void ResponsesJobs::Join() {
  if (state_->recovery.joinable()) state_->recovery.join();
  for (auto& worker : state_->workers)
    if (worker.joinable()) worker.join();
}

ResponsesJobsHealth ResponsesJobs::Health() const {
  const std::lock_guard lock(state_->mutex);
  ResponsesJobsHealth health;
  health.owned = state_->jobs.size();
  health.input_bytes = state_->bytes;
  health.stopping = state_->stopping;
  health.failed = state_->failed;
  health.recovery_configured = state_->recovery_configured;
  health.recovery_scanned = state_->recovery_scanned;
  health.recovered = state_->recovered;
  health.interrupted = state_->interrupted;
  health.recovery_errors = state_->recovery_errors;
  health.recovery_pending = state_->recovery_pending;
  health.rate_deferrals = state_->rate_deferrals;
  health.foreground_reconciled = state_->foreground_reconciled;
  health.foreground_errors = state_->foreground_errors;
  for (const auto& item : state_->foreground) {
    if (!item.ticket) continue;
    ++health.foreground_owned;
    health.foreground_active += item.active;
    health.foreground_pending += !item.active;
  }
  for (const auto& [_, job] : state_->jobs) {
    health.rate_waiting += job->rate_waiting;
    health.queued += job->stage == Job::Stage::kQueued;
    health.running += job->stage == Job::Stage::kRunning;
    health.uncertain += job->stage == Job::Stage::kUncertain;
  }
  return health;
}
}  // namespace q4t::server
