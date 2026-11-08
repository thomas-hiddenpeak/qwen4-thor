#include "q4t/server/responses_jobs.h"

#include <algorithm>
#include <atomic>
#include <condition_variable>
#include <deque>
#include <mutex>
#include <thread>
#include <unordered_map>

#include "object_protocol.h"
#include "q4t/server/lifetime.h"

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
  ObjectStore& objects;
  ResponsesJobsOptions options;
  Executor executor;
  mutable std::mutex mutex;
  std::condition_variable changed;
  std::unordered_map<std::string, std::shared_ptr<Job>> jobs;
  std::deque<std::shared_ptr<Job>> queue;
  std::vector<std::thread> workers;
  size_t last_client = kMaxClients;
  size_t bytes = 0;
  std::atomic<bool> stopping{false};
  std::atomic<bool> failed{false};
  State(ObjectStore& store, ResponsesJobsOptions limits, Executor run)
      : objects(store), options(limits), executor(std::move(run)) {}
};

ResponsesJobs::ResponsesJobs(ObjectStore& objects, ResponsesJobsOptions options,
                             Executor executor)
    : state_(std::make_unique<State>(objects, options, std::move(executor))) {
  if (!options.workers || options.workers > 32 || !options.max_jobs ||
      options.max_jobs > 256 || options.workers > options.max_jobs ||
      !options.max_client_jobs || options.max_client_jobs > options.max_jobs ||
      !options.max_input_bytes || options.max_input_bytes > (256ULL << 20) ||
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

ObjectReply ResponsesJobs::Submit(std::unique_ptr<ResponsesExecution> run,
                                  ClientIdentity client,
                                  const TransferPermit& transfer) {
  if (!run || !run->Prepared() || run->Begun() || !run->Envelope().background ||
      !client.Allowed() || !transfer)
    return Error(500, "background_state_invalid", "Invalid background handoff");
  const auto scope = client.Scoped() ? "client:" + std::string(client.Id())
                                     : "local:anonymous";
  if (scope != run->Scope())
    return Error(403, "object_scope_mismatch", "Background client changed");
  auto job = std::make_shared<Job>();
  job->bytes = run->RetainedInputBytes();
  job->control = std::make_shared<RequestControl>(
      run->Id(), "", Clock::now() + state_->options.timeout, client.Id());
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
  std::string ignored;
  auto result = job->run->Begin(&ignored);
  if (!result.ok()) return result;
  if (!transfer(job->permit))
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

void ResponsesJobs::Release(const std::shared_ptr<Job>& job,
                            bool known_terminal) {
  const std::lock_guard lock(state_->mutex);
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
    if (!job->control->Cancelled()) {
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
      bool reconcile = false;
      {
        std::unique_lock lock(state_->mutex);
        state_->changed.wait_for(lock, std::chrono::milliseconds(250), [&] {
          return state_->stopping || !state_->queue.empty();
        });
        if (state_->queue.empty() && state_->stopping) return;
        for (const auto& [_, candidate] : state_->jobs) {
          if (candidate->stage == Job::Stage::kUncertain &&
              Clock::now() >= candidate->next_recovery) {
            job = candidate;
            job->stage = Job::Stage::kClosing;
            reconcile = true;
            break;
          }
        }
        if (!job && state_->queue.empty()) continue;
        if (!job) {
          const auto rank = [&](const auto& candidate) {
            return (candidate->client.Index() + kMaxClients -
                    state_->last_client) %
                   (kMaxClients + 1);
          };
          auto choice = std::min_element(
              state_->queue.begin(), state_->queue.end(),
              [&](const auto& a, const auto& b) { return rank(a) < rank(b); });
          job = *choice;
          state_->queue.erase(choice);
          state_->last_client = job->client.Index();
          job->stage = Job::Stage::kRunning;
          if (state_->stopping)
            job->control->Cancel(RequestControl::State::kShutdown);
        }
      }
      if (reconcile)
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
  for (const auto& [_, job] : state_->jobs) {
    health.queued += job->stage == Job::Stage::kQueued;
    health.running += job->stage == Job::Stage::kRunning;
    health.uncertain += job->stage == Job::Stage::kUncertain;
  }
  return health;
}
}  // namespace q4t::server
