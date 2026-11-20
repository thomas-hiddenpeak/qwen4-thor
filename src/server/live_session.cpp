#include "q4t/server/live_session.h"

#include <algorithm>
#include <limits>

#include "object_protocol.h"

namespace q4t::server {
namespace {
using namespace object_detail;
bool Accepted(const ObjectReply& reply) {
  return reply.ok() && !reply.commit_unknown && reply.http_status >= 200 &&
         reply.http_status < 300;
}
ObjectReply Invalid(std::string code, std::string message,
                    std::string param = {}, int http = 400) {
  auto result = object_detail::Error(http, std::move(code), std::move(message));
  result.value = Object({{"param", String(std::move(param))}});
  return result;
}
ObjectReply ShapeFailure(const Status& status, const ChatPlanError& error) {
  return Invalid(error.code, status.message(), error.param, error.http_status);
}
}  // namespace

LiveSession::LiveSession(std::string id, uint64_t expires_at,
                         WebSocketOutput& output,
                         LiveSessionCallbacks callbacks,
                         LiveSessionLimits limits)
    : id_(std::move(id)),
      expires_at_(expires_at),
      output_(output),
      callbacks_(std::move(callbacks)),
      limits_(limits) {}
LiveSession::~LiveSession() {
  Cancel();
  Join();
}
bool LiveSession::AdvanceClock() {
  try {
    const auto now = callbacks_.now ? callbacks_.now() : Clock::now();
    if (state_ != State::kCreated && now < now_) return Fail();
    now_ = now;
    if (callbacks_.interrupted && callbacks_.interrupted()) return Fail();
    return true;
  } catch (...) {
    return Fail();
  }
}
bool LiveSession::Start() {
  if (state_ != State::kCreated || !ValidApplicationId(id_) || !expires_at_ ||
      expires_at_ > 9007199254740991ULL || !output_.Valid() ||
      output_.Closed() || !callbacks_.create ||
      !callbacks_.configuration.count_text ||
      !callbacks_.configuration.count_history ||
      !callbacks_.configuration.validate ||
      callbacks_.configuration.default_voice.IsNull() ||
      !limits_.message_bytes ||
      limits_.message_bytes > kWebSocketMessageLimit ||
      !limits_.deferred_bytes || limits_.deferred_bytes > (32ULL << 20) ||
      !limits_.deferred_events || limits_.deferred_events > 1024 ||
      limits_.startup.count() <= 0 || limits_.startup.count() > 60000 ||
      limits_.lifetime.count() <= 0 || limits_.lifetime.count() > 3600000 ||
      limits_.drain.count() <= 0 || limits_.drain.count() > 60000 ||
      limits_.output.count() <= 0 || limits_.output.count() > 60000 ||
      limits_.usage_interval.count() < 100 || !AdvanceClock())
    return false;
  born_ = now_;
  state_ = State::kWaiting;
  return true;
}
void LiveSession::Cancel() noexcept {
  if (state_ != State::kClosed) state_ = State::kCancelled;
  if (backend_ && !backend_cancelled_ && !joined_) {
    backend_cancelled_ = true;
    backend_->Cancel();
  }
  pending_.clear();
  deferred_bytes_ = 0;
}
void LiveSession::Join() noexcept {
  if (backend_ && !joined_) {
    backend_->Join();
    joined_ = true;
  }
}
bool LiveSession::Fail() noexcept {
  Cancel();
  return false;
}
bool LiveSession::Flush() {
  if (output_.Closed()) return Fail();
  while (!pending_.empty()) {
    const auto& front = pending_.front();
    if (now_ >= front.deadline) return Fail();
    if (!output_.TryPush(WebSocketOpcode::kText, front.text)) return true;
    if (front.terminal) finalized_.store(true);
    deferred_bytes_ -= front.charge;
    pending_.pop_front();
  }
  return true;
}
bool LiveSession::Emit(Json event, bool terminal) {
  if (state_ == State::kCancelled || state_ == State::kClosed) return false;
  const auto type = event.GetString("type");
  if (type != "session.output_audio.delta") {
    if (sequence_ == std::numeric_limits<uint64_t>::max()) return Fail();
    Set(&event, "event_id",
        String(id_ + "_event_" + std::to_string(++sequence_)));
  }
  ChatPlanError error;
  if (!ValidateLiveShape(event, LiveShape::kServerEvent, &error)) return Fail();
  auto text = OutputJson(event);
  if (text.size() > output_.MessageLimit() ||
      pending_.size() >= limits_.deferred_events ||
      sizeof(Pending) > limits_.deferred_bytes - deferred_bytes_ ||
      text.capacity() >
          limits_.deferred_bytes - deferred_bytes_ - sizeof(Pending))
    return Fail();
  const size_t charge = sizeof(Pending) + text.capacity();
  pending_.push_back(
      {std::move(text), charge, now_ + limits_.output, terminal});
  deferred_bytes_ += charge;
  return Flush();
}
ObjectReply LiveSession::BackendEvent(Json event) {
  const auto type = event.GetString("type");
  if ((state_ != State::kActive && state_ != State::kDraining) ||
      type == "session.started" || type == "session.updated" ||
      type == "session.closed" || type == "session.usage.updated" ||
      !Emit(std::move(event))) {
    backend_fault_ = true;
    return Invalid("live_backend_event_invalid", "Invalid native Live event",
                   {}, 503);
  }
  return {};
}
bool LiveSession::Error(ObjectReply reply, std::string_view client_event) {
  auto details = Object(
      {{"type", String(reply.http_status >= 500 ? "server_error"
                                                : "invalid_request_error")},
       {"code", String(reply.code.empty() ? "live_backend_unconfirmed"
                                          : reply.code.substr(0, 256))},
       {"message", String(reply.message.substr(0, 4096))}});
  const auto param = reply.value.GetString("param");
  if (!param.empty()) Set(&details, "param", String(param.substr(0, 512)));
  auto event = Object({{"type", String("error")}});
  if (!client_event.empty() && client_event.size() <= 256) {
    Set(&event, "client_event_id", String(std::string(client_event)));
    Set(&details, "client_event_id", String(std::string(client_event)));
  }
  Set(&event, "error", std::move(details));
  return Emit(std::move(event));
}
bool LiveSession::StartSession(const Json& event) {
  Json configuration, resource;
  ChatPlanError error;
  auto status = ResolveLiveConfiguration(
      *event.Find("session"), callbacks_.configuration, &configuration, &error);
  if (status)
    status =
        LiveSessionResource(configuration, id_, expires_at_, &resource, &error);
  const auto client_event = event.GetString("event_id");
  if (!status) return Error(ShapeFailure(status, error), client_event);
  std::unique_ptr<ILiveSessionBackend> backend;
  ObjectReply reply;
  try {
    reply = callbacks_.create(
        configuration,
        [this](Json value) { return BackendEvent(std::move(value)); },
        &backend);
  } catch (...) {
    reply = Invalid("live_backend_start_failed", "Native Live startup failed",
                    {}, 503);
  }
  if (!Accepted(reply) || !backend || backend_fault_) {
    if (backend) {
      backend->Cancel();
      backend->Join();
    }
    if (backend_fault_ || reply.commit_unknown) return Fail();
    if (Accepted(reply))
      reply = Invalid("live_backend_missing", "Native Live owner unavailable",
                      {}, 503);
    return Error(std::move(reply), client_event);
  }
  backend_ = std::move(backend);
  configuration_ = std::move(configuration);
  if (!AdvanceClock()) return false;
  if (now_ - born_ >= limits_.startup || now_ - born_ >= limits_.lifetime)
    return Fail();
  started_ = last_usage_ = now_;
  state_ = State::kActive;
  auto response = Object(
      {{"type", String("session.started")}, {"session", std::move(resource)}});
  if (!client_event.empty())
    Set(&response, "client_event_id", String(client_event));
  return Emit(std::move(response));
}
bool LiveSession::UpdateSession(const Json& event) {
  Json next, resource;
  ChatPlanError error;
  auto status =
      UpdateLiveConfiguration(configuration_, *event.Find("session"),
                              callbacks_.configuration, &next, &error);
  if (status)
    status = LiveSessionResource(next, id_, expires_at_, &resource, &error);
  const auto client_event = event.GetString("event_id");
  if (!status) return Error(ShapeFailure(status, error), client_event);
  const auto reply = backend_->Update(next);
  if (backend_fault_ || reply.commit_unknown) return Fail();
  if (!Accepted(reply)) return Error(reply, client_event);
  configuration_ = std::move(next);
  auto response = Object(
      {{"type", String("session.updated")}, {"session", std::move(resource)}});
  if (!client_event.empty())
    Set(&response, "client_event_id", String(client_event));
  return Emit(std::move(response));
}
bool LiveSession::BeginClose(std::string reason, std::string client_event) {
  state_ = State::kDraining;
  closing_ = now_;
  close_reason_ = std::move(reason);
  close_event_ = std::move(client_event);
  const auto reply = backend_->Close();
  if (!Accepted(reply) || backend_fault_) return Fail();
  return true;
}
bool LiveSession::Message(WebSocketOpcode opcode, std::string_view message) {
  if (state_ == State::kCreated || state_ == State::kCancelled ||
      !AdvanceClock() || !Flush())
    return false;
  // The socket may already have queued input when the worker finalized. Drop
  // that transport tail without accepting work or writing after session.closed.
  if (state_ == State::kClosed) return true;
  if (opcode != WebSocketOpcode::kText ||
      message.size() > limits_.message_bytes)
    return Error(
        Invalid("invalid_request", "Expected a bounded Live JSON event"));
  Json event;
  io::JsonParseLimits limits;
  limits.max_depth = 64;
  limits.max_values = 65536;
  limits.max_string_bytes = limits_.message_bytes;
  limits.reject_duplicate_keys = true;
  if (!io::ParseJson(std::string(message), &event, true, limits))
    return Error(Invalid("invalid_json", "Invalid Live event JSON"));
  const auto client_event = event.GetString("event_id");
  if (client_event.size() > 256)
    return Error(
        Invalid("invalid_request", "Event ID exceeds 256 bytes", "event_id"));
  ChatPlanError error;
  const auto status = ValidateLiveShape(event, LiveShape::kClientEvent, &error);
  if (!status) return Error(ShapeFailure(status, error), client_event);
  const auto type = event.GetString("type");
  try {
    if (state_ == State::kWaiting) {
      if (now_ - born_ >= limits_.startup) return Fail();
      return type == "session.start"
                 ? StartSession(event)
                 : Error(Invalid("session_not_started",
                                 "Send session.start first"),
                         client_event);
    }
    if (state_ == State::kDraining)
      return Error(Invalid("session_closing", "Live session is closing"),
                   client_event);
    if (now_ - born_ >= limits_.lifetime) {
      if (!BeginClose("expired", {})) return false;
      return Error(Invalid("session_closing", "Live session has expired"),
                   client_event);
    }
    if (type == "session.start")
      return Error(
          Invalid("session_already_started", "Live session already started"),
          client_event);
    if (type == "session.update") return UpdateSession(event);
    if (type == "session.close")
      return BeginClose("close_requested", client_event);
    const auto reply = backend_->Command(event);
    if (backend_fault_ || reply.commit_unknown) return Fail();
    return Accepted(reply) ? true : Error(reply, client_event);
  } catch (...) {
    return Fail();
  }
}
double LiveSession::Seconds() const {
  return std::chrono::duration<double>(now_ - started_).count();
}
bool LiveSession::Usage(bool terminal) {
  Json seconds;
  seconds.type = Json::Type::kNumber;
  seconds.number = Seconds();
  auto event = Object(
      {{"type", String(terminal ? "session.closed" : "session.usage.updated")},
       {"usage", Object({{"seconds", std::move(seconds)}})}});
  if (terminal) {
    Json resource;
    ChatPlanError error;
    if (!LiveSessionResource(configuration_, id_, expires_at_, &resource,
                             &error))
      return Fail();
    Set(&event, "session", std::move(resource));
    Set(&event, "reason", String(close_reason_));
    if (!close_event_.empty())
      Set(&event, "client_event_id", String(close_event_));
  }
  last_usage_ = now_;
  return Emit(std::move(event), terminal);
}
bool LiveSession::Tick() {
  if (state_ == State::kCreated || state_ == State::kCancelled ||
      !AdvanceClock() || !Flush())
    return false;
  if (state_ == State::kClosed) return true;
  if (state_ == State::kWaiting)
    return now_ - born_ < limits_.startup || Fail();
  try {
    if (state_ == State::kActive && now_ - born_ >= limits_.lifetime &&
        !BeginClose("expired", {}))
      return false;
    if (!Accepted(backend_->Tick()) || backend_fault_) return Fail();
    if (state_ == State::kDraining) {
      if (backend_->Drained()) {
        Join();
        if (!AdvanceClock() || !Usage(true)) return false;
        state_ = State::kClosed;
        return true;
      }
      if (now_ - closing_ >= limits_.drain) return Fail();
    }
    return now_ - last_usage_ < limits_.usage_interval || Usage(false);
  } catch (...) {
    return Fail();
  }
}
}  // namespace q4t::server
