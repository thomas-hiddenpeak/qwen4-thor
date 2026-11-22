#include "q4t/server/live_decision_delegate.h"

#include <algorithm>

#include "object_protocol.h"
#include "q4t/server/http_response.h"
#include "q4t/server/realtime_conversation.h"

namespace q4t::server {
namespace {
using namespace object_detail;
constexpr size_t kHistory = 8ULL << 20, kQueueBytes = 32ULL << 20;
constexpr size_t kJobs = 4, kTracking = 4096;
ObjectReply Capacity() {
  return Error(429, "live_decision_capacity",
               "Live Decision capacity exhausted");
}
Json Id(const std::string& value) {
  return value.empty() ? Json{} : String(value);
}
bool Accepted(const ObjectReply& reply) {
  return reply.ok() && !reply.commit_unknown && reply.http_status == 200;
}
}  // namespace

Status CheckLiveDecisionSettings(const Json& configuration,
                                 const ChatPlanConfig& catalog,
                                 ChatPlanError* error) {
  if (!error) return Status::Fail("Missing Decision validation owner");
  const auto check = [&](const Json& settings, const std::string& model,
                         const std::string& path) {
    Json request;
    const auto status =
        MakeLiveDecisionRequest(model, *settings.Find("questions"), Array(),
                                Array(), catalog, &request, error);
    if (!status) error->param = path + "." + error->param;
    return status;
  };
  const auto* delegation = configuration.Find("delegation");
  if (delegation && delegation->GetString("type") == "decisions") {
    const auto* settings = delegation->Find("decisions");
    if (!settings || !settings->Has("questions"))
      return Status::Fail("Missing Decision delegation settings");
    auto status = check(*settings, settings->GetString("model"),
                        "session.delegation.decisions");
    if (!status) return status;
  }
  const auto* triggers = configuration.GetArray("triggers");
  if (!triggers) return Status::Fail("Missing resolved trigger settings");
  for (size_t i = 0; i < triggers->array.size(); ++i) {
    const auto* settings = triggers->array[i].Find("decisions");
    if (!settings || !settings->Has("questions"))
      return Status::Fail("Missing trigger questions");
    auto status =
        check(*settings, catalog.model,
              "session.triggers[" + std::to_string(i) + "].decisions");
    if (!status) return status;
  }
  return {};
}

LiveDecisionDelegate::LiveDecisionDelegate(LiveDecisionNative native,
                                           LiveEventSink emit,
                                           ChatPlanConfig catalog,
                                           const LiveDecisionItems& items,
                                           const Json& initial, size_t limit)
    : native_(std::move(native)),
      emit_(std::move(emit)),
      catalog_(std::move(catalog)),
      items_(items),
      output_limit_(limit),
      conversation_(Array()),
      triggers_(Array()) {
  // Initial Live messages have already passed the native role and content
  // checks.
  for (const auto& item : initial.array)
    conversation_.array.push_back(Object(
        {{"role", *item.Find("role")},
         {"content", *item.Find("content")->array.front().Find("text")}}));
  history_full_ =
      !MeasureRealtimeStorage(conversation_, kHistory, &history_bytes_);
}
LiveDecisionDelegate::~LiveDecisionDelegate() {
  Cancel();
  Join();
}
void LiveDecisionDelegate::Settings(const Json& configuration) {
  InputsChanged();
  const auto& owner = *configuration.Find("delegation");
  delegation_ = owner.GetString("type") == "decisions"
                    ? *owner.Find("decisions")
                    : Json{};
  triggers_ = *configuration.Find("triggers");
}
ObjectReply LiveDecisionDelegate::RestoreHistory(const Json& history) {
  size_t charge = 0;
  if (!history.IsArray() || history.array.size() > kTracking ||
      !MeasureRealtimeStorage(history, kHistory, &charge))
    return Capacity();
  for (const auto& item : history.array) {
    const auto role = item.GetString("role");
    const auto* text = item.Find("content");
    if ((role != "user" && role != "developer" && role != "assistant") ||
        !text || !text->IsString())
      return Error(503, "invalid_recording", "Invalid saved Decision history");
  }
  conversation_ = history;
  history_bytes_ = charge;
  history_full_ = false;
  last_start_ms_ = -1;
  return {};
}
ObjectReply LiveDecisionDelegate::ForkHistory(Json* history) const {
  if (!history || history_full_) return Capacity();
  *history = conversation_;
  return {};
}
bool LiveDecisionDelegate::Known(std::string_view id) const {
  return delegations_.contains(id);
}
ObjectReply LiveDecisionDelegate::Create(std::string command, uint64_t offset) {
  if (closing_) return Error(409, "session_closing", "Live session is closing");
  if (delegation_.IsNull())
    return Error(400, "invalid_delegation_mode",
                 "Decision creation requires Decisions delegation");
  if (command.empty() || command.size() > 256)
    return Error(400, "invalid_request",
                 "Expected a nonempty Decision event ID");
  if (commands_.contains(command))
    return Error(400, "duplicate_event_id",
                 "Decision event ID has already been used");
  if (commands_.size() >= kTracking) return Capacity();
  commands_.insert(command);
  Job job;
  job.command = std::move(command);
  job.delegation = detail::NewRequestId("delegation_");
  return Queue(std::move(job), delegation_, {}, offset);
}
ObjectReply LiveDecisionDelegate::Delegate(std::string id, Json task,
                                           uint64_t offset) {
  if (delegation_.IsNull() || id.empty() || Known(id))
    return Error(409, "invalid_delegation_mode", "Invalid Decision delegation");
  Job job;
  job.delegation = std::move(id);
  return Queue(std::move(job), delegation_, std::move(task), offset);
}
ObjectReply LiveDecisionDelegate::Transcript(std::string type,
                                             std::string delta,
                                             std::string event_id,
                                             int64_t begin_ms) {
  if (closing_ || delta.empty()) return {};
  const bool user = type == "session.input_transcript.delta";
  if ((!user && type != "session.output_transcript.delta") || event_id.empty())
    return Error(500, "live_decision_source_invalid",
                 "Missing published transcript identity");
  if (!history_full_) {
    const std::string role = user ? "user" : "assistant";
    if (last_start_ms_ == begin_ms && !conversation_.array.empty() &&
        conversation_.array.back().GetString("role") == role) {
      if (delta.size() > kHistory - history_bytes_)
        history_full_ = true;
      else {
        Field(conversation_.array.back(), "content").str += delta;
        history_bytes_ += delta.size();
      }
    } else {
      auto item = Object(
          {{"role", String(role)}, {"content", String(std::move(delta))}});
      size_t charge = 0;
      if (conversation_.array.size() >= kTracking ||
          !MeasureRealtimeStorage(item, kHistory - history_bytes_, &charge))
        history_full_ = true;
      else {
        history_bytes_ += charge;
        conversation_.array.push_back(std::move(item));
        last_start_ms_ = begin_ms;
      }
    }
  }
  for (const auto& trigger : triggers_.array) {
    if (trigger.GetString("event_type") != type) continue;
    Job job;
    job.source = std::move(event_id);
    job.type = std::move(type);
    job.trigger = detail::NewRequestId("trg_");
    job.begin_ms = begin_ms;
    job.revision = revision_;
    auto reply = Queue(std::move(job), *trigger.Find("decisions"), {}, 0);
    if (!Accepted(reply)) return ErrorEvent(reply, {});
    break;
  }
  return {};
}
ObjectReply LiveDecisionDelegate::Queue(Job job, const Json& settings,
                                        Json task, uint64_t offset) {
  if (closing_) return Error(409, "session_closing", "Live session is closing");
  // Adjacent pending fragments of the same utterance share one evaluation.
  // Never replace an active job, a manual command or a different snapshot.
  const bool replace = !job.trigger.empty() &&
                       jobs_.size() > static_cast<size_t>(worker_.valid()) &&
                       jobs_.back().type == job.type &&
                       jobs_.back().begin_ms == job.begin_ms &&
                       jobs_.back().revision == job.revision;
  if (history_full_ || (!replace && jobs_.size() >= kJobs) ||
      (!job.delegation.empty() && delegations_.size() >= kTracking))
    return Capacity();
  auto input = conversation_;
  if (!task.IsNull()) input.array.push_back(std::move(task));
  ChatPlanError error;
  auto status = MakeLiveDecisionRequest(
      settings.GetString("model", catalog_.model), *settings.Find("questions"),
      std::move(input), items_.Snapshot(), catalog_, &job.request, &error);
  if (!status) {
    auto reply = Error(error.http_status, error.code, status.message());
    reply.value = Object({{"param", String(error.param)}});
    return reply;
  }
  if (!MeasureRealtimeStorage(
          job.request,
          std::min(kHistory,
                   kQueueBytes - bytes_ + (replace ? jobs_.back().charge : 0)),
          &job.charge))
    return Capacity();
  if (!job.delegation.empty()) {
    auto event =
        Object({{"type", String("session.delegation.created")},
                {"offset_ms", Number(offset)},
                {"delegation", Object({{"type", String("delegation")},
                                       {"id", String(job.delegation)},
                                       {"target", String("decisions")}})}});
    if (!job.command.empty())
      Set(&event, "client_event_id", String(job.command));
    auto sent = emit_(std::move(event));
    if (!Accepted(sent)) {
      sent.commit_unknown = true;
      return sent;
    }
    delegations_.insert(job.delegation);
  }
  job.deadline = std::chrono::steady_clock::now() +
                 std::chrono::milliseconds(catalog_.request_deadline_ms);
  if (replace) {
    bytes_ -= jobs_.back().charge;
    job.deadline = jobs_.back().deadline;
    jobs_.pop_back();
  }
  bytes_ += job.charge;
  jobs_.push_back(std::move(job));
  return {};
}
ObjectReply LiveDecisionDelegate::ErrorEvent(const ObjectReply& error,
                                             const std::string& command) {
  auto details = Object(
      {{"code", String(error.code)},
       {"type", String(error.http_status >= 500 ? "server_error"
                                                : "invalid_request_error")},
       {"message", String(error.message.substr(0, 4096))}});
  auto event = Object({{"type", String("error")}});
  if (!command.empty()) {
    Set(&event, "client_event_id", String(command));
    Set(&details, "client_event_id", String(command));
  }
  if (error.value.Has("param"))
    Set(&details, "param", *error.value.Find("param"));
  Set(&event, "error", std::move(details));
  return emit_(std::move(event));
}
void LiveDecisionDelegate::Release() noexcept {
  if (control_) {
    try {
      native_.release(control_);
    } catch (...) {
    }
    control_.reset();
  }
}
ObjectReply LiveDecisionDelegate::Tick(bool allow_start) {
  using namespace std::chrono_literals;
  if (cancelled_ || jobs_.empty()) return {};
  auto& job = jobs_.front();
  ObjectReply result;
  try {
    const bool expired = std::chrono::steady_clock::now() >= job.deadline;
    if (expired && control_) control_->Cancel(RequestControl::State::kDeadline);
    if (!worker_.valid() && expired)
      result = Error(408, "deadline_exceeded", "Queued Decision expired");
    else if (!worker_.valid()) {
      if (!allow_start) return {};
      const auto id = detail::NewRequestId();
      control_ = native_.acquire(id);
      if (!control_ || control_->id() != id)
        result = Error(503, "live_decision_owner_invalid",
                       "Could not register Decision");
      else {
        worker_ = std::async(
            std::launch::async,
            [this, request = std::move(job.request), control = control_] {
              return native_.execute(request, control);
            });
        return {};
      }
    } else {
      if (worker_.wait_for(0ms) != std::future_status::ready) return {};
      result = worker_.get();
    }
  } catch (...) {
    result = Error(503, "live_decision_failed", "Native evaluation failed");
  }
  Release();
  ObjectReply sent;
  ChatPlanError error;
  if (Accepted(result) &&
      !ValidateDecisionShape(result.value, DecisionShape::kResult, &error))
    result =
        Error(502, "live_decision_invalid", "Invalid native Decision result");
  if (Accepted(result)) {
    auto event =
        Object({{"type", String("decision.event")},
                {"delegation_id", Id(job.delegation)},
                {"source_event_id", Id(job.source)},
                {"source_event_type", Id(job.type)},
                {"trigger_id", Id(job.trigger)},
                {"event", Object({{"type", String("decision.completed")},
                                  {"decision", std::move(result.value)}})}});
    // Leave room for the session-assigned event ID and JSON framing.
    if (OutputJson(event).size() + 256 > output_limit_)
      result = Error(413, "live_decision_output_capacity",
                     "Decision exceeds Live output capacity");
    else
      sent = emit_(std::move(event));
  }
  if (!Accepted(result)) sent = ErrorEvent(result, job.command);
  bytes_ -= job.charge;
  jobs_.pop_front();
  return sent;
}
void LiveDecisionDelegate::Cancel() noexcept {
  closing_ = cancelled_ = true;
  if (control_) control_->Cancel(RequestControl::State::kDisconnect);
}
void LiveDecisionDelegate::Join() noexcept {
  if (worker_.valid()) {
    try {
      worker_.get();
    } catch (...) {
    }
  }
  Release();
  jobs_.clear();
  bytes_ = 0;
}
}  // namespace q4t::server
