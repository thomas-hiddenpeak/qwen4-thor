#include "q4t/server/realtime_transcription_session.h"

#include "object_protocol.h"
#include "q4t/server/http_response.h"

namespace q4t::server {
namespace {
using namespace object_detail;
using Clock = std::chrono::steady_clock;
Json Event(std::string type) {
  return Object({{"type", String(std::move(type))},
                 {"event_id", String(detail::NewRequestId("event_"))}});
}
Json Failure(const ObjectReply& reply) {
  return Object(
      {{"type", String(reply.http_status >= 500 ? "server_error"
                                                : "invalid_request_error")},
       {"code", String(std::string(reply.ClientCode()))},
       {"message", String(reply.ClientMessage())},
       {"param",
        reply.value.Has("param") ? *reply.value.Find("param") : Json{}}});
}
}  // namespace

RealtimeTranscriptionSession::RealtimeTranscriptionSession(
    std::string id, WebSocketOutput& output,
    RealtimeTranscriptionCallbacks callbacks, uint32_t max_audio_seconds,
    size_t message_bytes)
    : id_(std::move(id)),
      output_(output),
      callbacks_(std::move(callbacks)),
      max_item_ticks_(uint64_t(max_audio_seconds) * kRealtimeTicksPerSecond),
      message_bytes_(message_bytes) {}

RealtimeTranscriptionSession::~RealtimeTranscriptionSession() {
  Cancel();
  Join();
}
void RealtimeTranscriptionSession::Release() noexcept {
  if (!control_) return;
  try {
    if (callbacks_.release) callbacks_.release(control_);
  } catch (...) {
  }
  control_.reset();
}
void RealtimeTranscriptionSession::Cancel() noexcept {
  cancelled_ = true;
  if (control_) control_->Cancel(RequestControl::State::kDisconnect);
  queue_.Cancel(1);
}
void RealtimeTranscriptionSession::Join() noexcept {
  if (worker_.joinable()) worker_.join();
  Release();
}
bool RealtimeTranscriptionSession::Emit(Json event) {
  return output_.TryPush(WebSocketOpcode::kText, OutputJson(event));
}
bool RealtimeTranscriptionSession::ErrorEvent(ObjectReply reply,
                                              std::string_view event_id) {
  auto error = Failure(reply);
  const auto id = event_id.empty() ? reply.value.GetString("event_id")
                                   : std::string(event_id);
  if (!id.empty()) Set(&error, "event_id", String(id));
  auto event = Event("error");
  Set(&event, "error", std::move(error));
  return Emit(std::move(event));
}
bool RealtimeTranscriptionSession::SettingsEvent(bool legacy, bool created) {
  auto session = RealtimeTranscriptionSettingsJson(settings_, legacy);
  Set(&session, "id", String(id_));
  Set(&session, "object", String("realtime.transcription_session"));
  auto event =
      Event(std::string(legacy ? "transcription_session." : "session.") +
            (created ? "created" : "updated"));
  Set(&event, "session", std::move(session));
  return Emit(std::move(event));
}
bool RealtimeTranscriptionSession::Start() {
  if (started_ || cancelled_ || !queue_.Valid() || id_.empty() ||
      !output_.Valid() || max_item_ticks_ == 0 ||
      max_item_ticks_ > 600 * kRealtimeTicksPerSecond || message_bytes_ == 0 ||
      message_bytes_ > kWebSocketMessageLimit || !callbacks_.validate ||
      !callbacks_.acquire || !callbacks_.release || !callbacks_.execute)
    return false;
  if (!callbacks_.validate(settings_).ok()) return false;
  started_ = true;
  return SettingsEvent(false, true);
}
bool RealtimeTranscriptionSession::Message(WebSocketOpcode opcode,
                                           std::string_view message) {
  if (!started_ || cancelled_) return false;
  if (opcode != WebSocketOpcode::kText)
    return ErrorEvent(Error(400, "invalid_event", "JSON text event required"));
  RealtimeTranscriptionCommand command;
  auto reply =
      ParseRealtimeTranscriptionCommand(message, message_bytes_, &command);
  if (!reply.ok()) return ErrorEvent(std::move(reply));
  const auto fail = [&](std::string code, std::string text) {
    return ErrorEvent(Error(400, std::move(code), std::move(text)),
                      command.event_id);
  };
  using K = RealtimeTranscriptionCommandKind;
  if (command.kind == K::kUpdate) {
    RealtimeTranscriptionSettings next;
    reply = UpdateRealtimeTranscriptionSettings(command.session, command.legacy,
                                                settings_, &next);
    if (reply.ok()) reply = callbacks_.validate(next);
    if (!reply.ok()) return ErrorEvent(std::move(reply), command.event_id);
    if (next.format != settings_.format) {
      const auto status = queue_.ChangeFormat(1, next.format);
      if (!status)
        return fail("input_audio_buffer_not_empty", status.message());
    }
    settings_ = std::move(next);
    return SettingsEvent(command.legacy);
  }
  if (command.kind == K::kAppend) {
    const size_t width =
        settings_.format.encoding == RealtimeAudioEncoding::kPcm16 ? 2 : 1;
    const auto samples = (queue_.PendingBytes() + command.audio.size()) / width;
    if (samples >
        max_item_ticks_ / (kRealtimeTicksPerSecond / settings_.format.rate))
      return fail("audio_duration_limit",
                  "Uncommitted audio duration exceeded");
    const auto status = queue_.Append(
        1, {reinterpret_cast<const uint8_t*>(command.audio.data()),
            command.audio.size()});
    return status ? true : fail("input_audio_buffer_full", status.message());
  }
  if (command.kind == K::kClear) {
    const auto status = queue_.Clear(1);
    if (!status) return fail("input_audio_buffer_invalid", status.message());
    return Emit(Event("input_audio_buffer.cleared"));
  }
  // Allocate the snapshot and identities before mutating the committed queue.
  RealtimeTranscriptionJob job;
  job.settings = settings_;
  job.id = detail::NewRequestId("item_");
  job.previous_id = previous_id_;
  jobs_.push_back(std::move(job));
  auto& last = jobs_.back();
  const auto status = queue_.Commit(1, &last.audio);
  if (!status) {
    jobs_.pop_back();
    return fail("input_audio_buffer_commit_failed", status.message());
  }
  previous_id_ = last.id;
  auto committed = Event("input_audio_buffer.committed");
  Set(&committed, "item_id", String(last.id));
  Set(&committed, "previous_item_id",
      last.previous_id.empty() ? Json{} : String(last.previous_id));
  if (!Emit(std::move(committed))) return false;
  auto created = Event("conversation.item.created");
  Set(&created, "previous_item_id",
      last.previous_id.empty() ? Json{} : String(last.previous_id));
  Set(&created, "item",
      Object({{"id", String(last.id)},
              {"object", String("realtime.item")},
              {"type", String("message")},
              {"role", String("user")},
              {"status", String("completed")},
              {"content", Array({Object({{"type", String("input_audio")},
                                         {"transcript", Json{}}})})}}));
  return Emit(std::move(created));
}

bool RealtimeTranscriptionSession::Tick() {
  if (!started_ || cancelled_) return false;
  if (worker_.joinable() && done_.load(std::memory_order_acquire)) {
    worker_.join();
    if (result_.ok() && (!result_.value.Find("text") ||
                         !result_.value.Find("text")->IsString() ||
                         !result_.value.Find("usage") ||
                         !result_.value.Find("usage")->IsObject()))
      result_ = Error(502, "transcription_result_invalid",
                      "Transcription text or accounting missing");
    if (result_.ok() && (control_->Cancelled() || !control_->Finish()))
      result_ = Error(409, control_->Reason(), "Transcription cancelled");
    auto event =
        Event(std::string("conversation.item.input_audio_transcription.") +
              (result_.ok() ? "completed" : "failed"));
    Set(&event, "item_id", String(jobs_.front().id));
    Set(&event, "content_index", Number(0));
    if (result_.ok()) {
      Set(&event, "transcript", String(result_.value.GetString("text")));
      for (const auto key : {"usage", "logprobs"})
        if (const auto* value = result_.value.Find(key))
          Set(&event, key, *value);
    } else {
      Set(&event, "error", Failure(result_));
    }
    terminal_ = OutputJson(event);
    terminal_deadline_ = Clock::now() + std::chrono::seconds(15);
    Release();
  }
  if (!terminal_.empty()) {
    if (terminal_.size() > kWebSocketOutputMessageBytes || output_.Closed() ||
        Clock::now() >= terminal_deadline_)
      return false;
    if (!output_.TryPush(WebSocketOpcode::kText, terminal_)) return true;
    terminal_.clear();
    const auto& item = jobs_.front().audio;
    if (!queue_.Acknowledge(item.epoch, item.sequence)) return false;
    jobs_.pop_front();
  }
  if (worker_.joinable() || jobs_.empty()) return true;
  const auto* audio = queue_.Front();
  if (!audio || *audio != jobs_.front().audio) return false;
  control_ = callbacks_.acquire(jobs_.front().id);
  if (!control_) return false;
  // Preallocate the exception fallback before starting the worker. Exception
  // handling inside the producer must not allocate during host exhaustion.
  result_ =
      Error(500, "transcription_failed", "Transcription execution failed");
  done_.store(false, std::memory_order_relaxed);
  const auto bytes = queue_.FrontBytes();
  const auto* borrowed_job = &jobs_.front();
  worker_ = std::thread([this, bytes, borrowed_job] {
    const auto& job = *borrowed_job;
    try {
      const auto delta = [&](const Json& part) {
        auto event = Event("conversation.item.input_audio_transcription.delta");
        Set(&event, "item_id", String(job.id));
        Set(&event, "content_index", Number(0));
        Set(&event, "delta", String(part.GetString("delta")));
        if (const auto* p = part.Find("logprobs")) Set(&event, "logprobs", *p);
        return output_.Push(WebSocketOpcode::kText, OutputJson(event),
                            *control_);
      };
      result_ = callbacks_.execute(job, bytes, control_, delta);
    } catch (...) {
      // Preserve the preallocated failure; no success from a partial stream.
    }
    done_.store(true, std::memory_order_release);
  });
  return true;
}
}  // namespace q4t::server
