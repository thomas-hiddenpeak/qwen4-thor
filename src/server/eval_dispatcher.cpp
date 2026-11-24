#include "q4t/server/eval_dispatcher.h"

#include <algorithm>
#include <array>

#include "eval_run_state.h"
#include "eval_model_grading.h"
#include "q4t/server/lifetime.h"
#include "eval_sampling.h"

namespace q4t::server {
using namespace std::chrono_literals;
EvalDispatcher::EvalDispatcher(ObjectStore& objects, ClientAdmission& clients,
                               ChatPlanConfig config,
                               std::function<ResponsesJobs*()> jobs,
                               Embed embed,
                               std::chrono::milliseconds model_timeout)
    : config_(std::move(config)),
      jobs_(std::move(jobs)),
      embed_(std::move(embed)),
      model_timeout_(model_timeout),
      thread_([this, &objects, &clients](std::stop_token stop) {
        Run(objects, clients, stop);
      }) {}
EvalDispatcher::~EvalDispatcher() {
  RequestStop();
  Join();
}

void EvalDispatcher::Wake() {
  {
    std::lock_guard lock(mutex_);
    ++epoch_;
  }
  changed_.notify_one();
}
void EvalDispatcher::CancelEmbedding(std::string_view scope,
                                     std::string_view eval,
                                     std::string_view run) {
  std::lock_guard lock(mutex_);
  if (active_embedding_ && active_scope_ == scope && active_eval_ == eval &&
      (run.empty() || active_run_ == run))
    active_embedding_->Cancel();
}
void EvalDispatcher::RequestStop() {
  thread_.request_stop();
  {
    std::lock_guard lock(mutex_);
    if (active_embedding_)
      active_embedding_->Cancel(RequestControl::State::kShutdown);
  }
  changed_.notify_all();
}
void EvalDispatcher::Join() {
  if (thread_.joinable()) thread_.join();
}

ObjectReply EvalDispatcher::EmbedPair(
    ObjectStore& objects, ClientAdmission& clients, const ClientIdentity& owner,
    const StateAccess& access, const io::Json& record, const io::Json& input) {
  using namespace object_detail;
  if (!embed_)
    return Error(422, "eval_embedding_model_unconfigured",
                 "Cosine grading requires the native trained embedding model");
  ClientLease permit;
  if (!permit.Acquire(clients, owner, ClientLane::kData))
    return Error(429, "client_capacity_exhausted",
                 "Evaluation client is at capacity");
  const auto id = record.GetString("id");
  const auto eval = record.Find("data")->Find("resource")->GetString("eval_id");
  const auto deadline = std::min(
      access.deadline, std::chrono::steady_clock::now() + model_timeout_);
  auto control = std::make_shared<RequestControl>(id, "", deadline, owner.Id());
  {
    std::lock_guard lock(mutex_);
    active_scope_ = access.scope;
    active_eval_ = eval;
    active_run_ = id;
    active_embedding_ = control;
  }
  ScopeExit release([&] {
    std::lock_guard lock(mutex_);
    active_embedding_.reset();
  });
  // Register cancellation before checking the revision to cover a cancel or
  // delete between the dispatcher scan and model admission.
  auto current =
      objects.Execute(eval_detail::Read(ObjectKind::kEvalRun, access, id),
                      access.deadline, access.cancelled);
  if (!current.ok()) return current;
  if (control->Cancelled() || (access.cancelled && access.cancelled()) ||
      current.value.GetInt("revision") != record.GetInt("revision"))
    return Error(409, "eval_run_changed",
                 "Evaluation run changed before grading");
  return embed_(input, owner, permit, control);
}

void EvalDispatcher::Run(ObjectStore& objects, ClientAdmission& clients,
                         std::stop_token stop) {
  struct Cursor {
    std::string scope, after;
  };
  std::array<Cursor, kMaxClients> cursors;
  const auto canceled = [&] { return stop.stop_requested(); };
  while (!canceled()) {
    uint64_t epoch;
    {
      std::lock_guard lock(mutex_);
      epoch = epoch_;
    }
    auto delay = 2000ms;
    try {
      const auto count = std::max<size_t>(1, clients.Size());
      for (size_t i = 0; i < count && !canceled(); ++i) {
        auto& cursor = cursors.at(i);
        const auto owner = clients.RecoveryIdentity(i);
        if (!owner.Allowed()) {
          cursor = {};
          continue;
        }
        auto scope = owner.Scoped() ? "client:" + std::string(owner.Id())
                                    : "local:anonymous";
        if (scope != cursor.scope) cursor = {scope, {}};
        StateAccess access{scope, std::chrono::steady_clock::now() + 8s,
                           canceled};
        ObjectRequest scan;
        scan.scope = scope;
        scan.kind = ObjectKind::kEvalRun;
        scan.operation = ObjectOperation::kList;
        scan.after = cursor.after;
        scan.limit = 4;
        scan.descending = false;
        auto page = objects.Execute(scan, access.deadline, canceled);
        if (!page.ok()) {
          if (page.code == "invalid_object_cursor" || page.http_status == 404)
            cursor.after.clear();
          if (!canceled()) ++errors_;
          continue;
        }
        const auto* rows = page.value.GetArray("data");
        if (!rows) {
          ++errors_;
          continue;
        }
        cursor.after = page.value.GetBool("has_more")
                           ? page.value.GetString("last_id")
                           : "";
        if (!cursor.after.empty()) delay = 25ms;
        for (const auto& row : rows->array) {
          const auto* data = row.Find("data");
          const auto* run = data ? data->Find("resource") : nullptr;
          if (!run) {
            ++errors_;
            continue;
          }
          const auto status = run->GetString("status");
          if (!data->GetBool("deleting") && !data->Has("pending_response") &&
              !data->Has("pending_input") &&
              status != "in_progress" &&
              status != "queued")
            continue;
          std::optional<eval_detail::SamplingOwner> sampler;
          auto turn = access;
          if (auto* jobs = jobs_()) {
            sampler.emplace(clients, owner, *jobs, config_);
            const auto* criteria = data->GetArray("criteria");
            if (criteria &&
                std::any_of(criteria->array.begin(), criteria->array.end(),
                            eval_detail::CosineCriterion)) {
              turn.deadline =
                  std::chrono::steady_clock::now() + model_timeout_ + 8s;
              sampler->embed = [&](const io::Json& input) {
                return EmbedPair(objects, clients, owner, turn, row, input);
              };
            }
          }
          auto result = eval_detail::AdvanceRun(objects, turn, row,
                                                sampler ? &*sampler : nullptr);
          if (result.ok())
            delay = std::min(delay, result.http_status == 202 ? 150ms : 25ms);
          else if (result.http_status != 409 && result.http_status != 404 &&
                   !canceled())
            ++errors_;
        }
      }
    } catch (const std::exception&) {
      ++errors_;
      delay = 2000ms;
    }
    std::unique_lock lock(mutex_);
    changed_.wait_for(lock, stop, delay, [&] { return epoch != epoch_; });
  }
}
}  // namespace q4t::server
