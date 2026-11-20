#include "q4t/server/realtime_conversation_input.h"

#include "object_protocol.h"
#include "q4t/io/base64.h"
#include "q4t/server/asr_response.h"
#include "q4t/server/http_response.h"
#include "q4t/server/realtime_audio_export.h"

namespace q4t::server {
namespace {
using namespace object_detail;
bool Accepted(const ObjectReply& reply) {
  return reply.ok() && !reply.commit_unknown && reply.http_status >= 200 &&
         reply.http_status < 300;
}
Json Event(std::string type) {
  return Object({{"type", String(std::move(type))},
                 {"event_id", String(detail::NewRequestId("event_"))}});
}
ObjectReply Missing() {
  return Error(400, "realtime_audio_item_not_found", "Audio item not found");
}
bool CaptureBounded(const RealtimeInputCommit& notice) {
  // Fixed ledger/ticket metadata is bounded by 128 items. Include actual JSON
  // capacities and the copied native settings before retaining any capture.
  size_t total = 64ULL << 10;
  auto processing =
      RealtimeTranscriptionSettingsJson(notice.settings.processing, false);
  std::vector<const Json*> values{&notice.context.items, &processing};
  if (notice.response) {
    values.push_back(&notice.response->settings);
    if (notice.response->prompt) {
      values.push_back(&notice.response->prompt->reference);
      values.push_back(&notice.response->prompt->request);
      total += notice.response->prompt->scope.capacity();
    }
  }
  for (const auto* value : values) {
    size_t bytes = 0;
    if (total > (96ULL << 20) ||
        !MeasureRealtimeStorage(*value, (96ULL << 20) - total, &bytes))
      return false;
    total += bytes;
  }
  return true;
}
}  // namespace

RealtimeConversationInput::RealtimeConversationInput(
    std::string id, RealtimeConversation& history,
    RealtimeTranscriptionCallbacks native,
    RealtimeConversationInputCallbacks callbacks, uint32_t max_audio_seconds,
    size_t message_bytes)
    : history_(history),
      callbacks_(std::move(callbacks)),
      message_bytes_(message_bytes),
      processor_(std::move(id), output_, Bind(std::move(native)),
                 max_audio_seconds, message_bytes) {}
RealtimeConversationInput::~RealtimeConversationInput() {
  Cancel();
  Join();
}
RealtimeTranscriptionCallbacks RealtimeConversationInput::Bind(
    RealtimeTranscriptionCallbacks native) {
  interrupted_ = native.interrupted;
  native.owner = this;
  return native;
}
ObjectReply RealtimeConversationInput::Configure(RealtimeDuplexInput next) {
  if (cancelled_)
    return Error(409, "realtime_connection_closed", "Input owner stopped");
  if (!next.enabled) {
    if (!failed_ && (processor_.QueuedItems() || processor_.AudioBytes()))
      return Error(400, "input_audio_buffer_not_empty",
                   "Commit or clear pending audio before disabling input");
    settings_ = std::move(next);
    CancelIdle();
    return {};
  }
  if (failed_) return RecoverInput(std::move(next));
  if (!callbacks_.emit || !callbacks_.ready || !callbacks_.speech ||
      !callbacks_.committed || !history_.Valid())
    return Error(503, "realtime_processor_unavailable",
                 "Conversation input owner is not connected");
  auto reply = processor_.Configure(next.processing);
  if (!Accepted(reply)) return reply;
  const bool idle_changed = next.idle_timeout_ms != settings_.idle_timeout_ms ||
                            next.processing.turn != settings_.processing.turn;
  if (!started_) {
    if (!processor_.Start())
      return Error(503, "realtime_processor_unavailable",
                   "Native input processor could not start");
    started_ = true;
  }
  retrieval_format_ = next.processing.format;
  settings_ = std::move(next);
  if (idle_changed) ArmIdle();
  return {};
}
bool RealtimeConversationInput::ErrorEvent(ObjectReply reply,
                                           std::string_view event_id) {
  if (reply.code.empty())
    reply = Error(503, "realtime_input_unconfirmed",
                  "Native input operation was not confirmed");
  auto error = Object(
      {{"type", String(reply.http_status >= 500 ? "server_error"
                                                : "invalid_request_error")},
       {"code", String(std::string(reply.ClientCode()))},
       {"message", String(reply.ClientMessage())}});
  if (!event_id.empty()) Set(&error, "event_id", String(std::string(event_id)));
  if (const auto* param = reply.value.Find("param"))
    Set(&error, "param", *param);
  auto event = Event("error");
  Set(&event, "error", std::move(error));
  return callbacks_.emit && Accepted(callbacks_.emit(event));
}
bool RealtimeConversationInput::Message(std::string_view message) {
  if (cancelled_ || message.size() > message_bytes_) return false;
  Json parsed;
  if (!io::ParseJson(std::string(message), &parsed, true,
                     {64, 65536, message_bytes_, true}))
    return ErrorEvent(Error(400, "invalid_json", "Invalid input event"));
  const auto type = parsed.GetString("type");
  const auto event_id = parsed.GetString("event_id");
  if (type != "input_audio_buffer.append" &&
      type != "input_audio_buffer.commit" && type != "input_audio_buffer.clear")
    return ErrorEvent(Error(400, "realtime_input_event_invalid",
                            "Expected an audio buffer event"),
                      event_id);
  if (!started_ || !settings_.enabled || failed_)
    return ErrorEvent(Error(503, "realtime_input_unavailable",
                            "Native audio input is not available"),
                      event_id);
  if (!Drain()) return false;
  StartIdleCapture();
  const bool alive = processor_.Message(WebSocketOpcode::kText, message);
  if (!Drain()) return false;
  return alive || StopInput();
}
bool RealtimeConversationInput::Tick() {
  if (cancelled_) return false;
  if (failed_) {
    if (!processor_.TryJoinCancelled() || !settings_.enabled ||
        Now() < retry_after_)
      return true;
    const auto recovered = RecoverInput(settings_);
    // A circuit pause is not another failed execution. Actual failed recovery
    // is reported once per bounded attempt; no input or model work is replayed.
    if (!Accepted(recovered) && recovered.code != "realtime_input_cooldown")
      return ErrorEvent(recovered);
    return true;
  }
  if (!started_) return true;
  if (!Drain()) return false;
  StartIdleCapture();
  const bool alive = processor_.Tick();
  if (!Drain()) return false;
  return alive ? TickIdle() : StopInput();
}
void RealtimeConversationInput::ResponseFinished(uint64_t playback_ticks) {
  CancelIdle();
  if (failed_ || cancelled_ || playback_ticks > kRealtimeSessionTicks) return;
  playback_end_ = Now() + std::chrono::microseconds(
                              (playback_ticks * 1000000 + 47999) / 48000);
  ArmIdle();
}
void RealtimeConversationInput::ArmIdle() {
  processor_.CancelIdleCapture();
  idle_capturing_ = false;
  idle_deadline_.reset();
  if (!failed_ && !cancelled_ && settings_.enabled && playback_end_ &&
      settings_.processing.turn ==
          RealtimeTranscriptionSettings::Turn::kServerVad &&
      settings_.idle_timeout_ms)
    idle_deadline_ =
        *playback_end_ + std::chrono::milliseconds(*settings_.idle_timeout_ms);
}
void RealtimeConversationInput::CancelIdle() noexcept {
  playback_end_.reset();
  idle_deadline_.reset();
  idle_capturing_ = false;
  processor_.CancelIdleCapture();
}
void RealtimeConversationInput::StartIdleCapture() {
  if (idle_deadline_ && !idle_capturing_ && Now() >= *playback_end_) {
    processor_.BeginIdleCapture();
    idle_capturing_ = true;
  }
}
bool RealtimeConversationInput::TickIdle() {
  if (!idle_deadline_ || Now() < *idle_deadline_ || !processor_.IdleReady())
    return true;
  // Commit calls our owner and disarms this timer. The next output completion
  // may arm a fresh interval; no periodic replay of the old response.
  const bool alive = processor_.CommitIdle();
  if (!Drain()) return false;
  return alive || StopInput();
}
void RealtimeConversationInput::FailPending() noexcept {
  for (auto& [id, binding] : bindings_) {
    (void)id;
    if (binding.settled) continue;
    try {
      history_.FailAudioInput(binding.conversation);
    } catch (...) {
    }
    binding.settled = true;
    ReleaseNotice(binding);
  }
}
bool RealtimeConversationInput::StopInput() {
  failed_ = true;
  CancelIdle();
  processor_.Cancel();
  FailPending();
  retry_after_ = Now() + AsrHealth::kRetryDelay;
  return ErrorEvent(Error(503, "realtime_input_failed",
                          "Native audio input stopped; text remains usable"));
}
std::chrono::steady_clock::time_point RealtimeConversationInput::Now() const {
  return callbacks_.now ? callbacks_.now() : std::chrono::steady_clock::now();
}
ObjectReply RealtimeConversationInput::RecoverInput(RealtimeDuplexInput next) {
  if (!failed_ || cancelled_ || !processor_.TryJoinCancelled())
    return Error(409, "realtime_input_still_borrowed",
                 "Cancelled input work has not exited");
  const auto now = Now();
  if (now < retry_after_)
    return Error(503, "realtime_input_cooldown",
                 "Input recovery is cooling down");
  if (now >= recovery_window_ + std::chrono::minutes(1)) {
    recovery_window_ = now;
    recovery_attempts_ = 0;
  }
  if (recovery_attempts_ >= 2) {
    retry_after_ = recovery_window_ + std::chrono::minutes(1);
    return Error(503, "realtime_input_cooldown",
                 "Input recovery circuit is open");
  }
  ++recovery_attempts_;
  retry_after_ = now + AsrHealth::kRetryDelay;
  ObjectReply reply;
  try {
    reply = processor_.Recover(next.processing);
  } catch (...) {
    reply = Error(503, "realtime_input_recovery_failed",
                  "Native input recovery did not confirm success");
  }
  if (!Accepted(reply)) return reply;
  // The cancelled worker has joined. Discard only its unconsumed private
  // mailbox; old failed turns stay failed/retrievable in the common history.
  while (!output_.Front().empty()) output_.Acknowledge();
  retrieval_format_ = next.processing.format;
  settings_ = std::move(next);
  failed_ = false;
  return {};
}
void RealtimeConversationInput::Cancel() noexcept {
  cancelled_ = true;
  CancelIdle();
  processor_.Cancel();
  FailPending();
}
void RealtimeConversationInput::Join() noexcept { processor_.Join(); }

ObjectReply RealtimeConversationInput::Reserve(
    const RealtimeTranscriptionJob& job) {
  if (!settings_.enabled || failed_ || cancelled_ || !job.processed_audio ||
      bindings_.size() == RealtimeConversation::kMaximumItems ||
      bindings_.contains(job.id))
    return Error(503, "realtime_input_owner_unavailable",
                 "Native input owner is unavailable");
  Binding binding;
  binding.input = job.transcript;
  binding.audio = job.processed_audio;
  binding.settings = settings_;
  const auto [found, inserted] = bindings_.emplace(job.id, std::move(binding));
  if (!inserted) return Missing();
  const auto status = history_.ReserveAudioInput(
      job.id, settings_.transcript_events, kAsrTranscriptBytes + 8192,
      &found->second.conversation);
  if (!status) {
    bindings_.erase(found);
    return Error(429, "conversation_capacity_exceeded", status.message());
  }
  return {};
}
ObjectReply RealtimeConversationInput::Commit(
    const RealtimeTranscriptionJob& job, bool automatic,
    std::string* previous_id) {
  const auto found = bindings_.find(job.id);
  if (found == bindings_.end() || found->second.input != job.transcript ||
      found->second.committed || !previous_id)
    return Missing();
  auto& binding = found->second;
  RealtimeInputCommit notice;
  notice.ticket = binding.conversation;
  notice.settings = binding.settings;
  notice.automatic = automatic;
  auto previous = history_.LastId();
  const auto status = history_.CommitAudioInput(binding.conversation);
  if (!status)
    return Error(400, "realtime_input_commit_failed", status.message());
  binding.committed = true;
  CancelIdle();
  if (notice.settings.RespondOnCommit(automatic)) {
    const auto captured = history_.Capture(nullptr, &notice.context, true);
    if (!captured) {
      notice.context_status =
          Error(400, "realtime_input_context_invalid", captured.message());
    } else if (callbacks_.capture_response) {
      notice.response.emplace();
      notice.context_status = callbacks_.capture_response(&*notice.response);
    }
    if (Accepted(notice.context_status) &&
        (captures_ == 4 || !CaptureBounded(notice)))
      notice.context_status =
          Error(429, "realtime_response_capture_capacity",
                "Pending automatic response storage exhausted");
    if (Accepted(notice.context_status)) {
      binding.captured = true;
      ++captures_;
    } else {
      notice.context = {};
      notice.response.reset();
    }
  }
  binding.notice = std::move(notice);
  *previous_id = std::move(previous);
  return {};
}
ObjectReply RealtimeConversationInput::Finish(
    const RealtimeTranscriptionJob& job, const ObjectReply& result) {
  const auto found = bindings_.find(job.id);
  if (found == bindings_.end() || found->second.input != job.transcript ||
      found->second.settled)
    return Missing();
  auto& binding = found->second;
  auto status = Accepted(result)
                    ? history_.CompleteAudioInput(
                          binding.conversation, result.value.GetString("text"),
                          job.idle_timeout)
                    : history_.FailAudioInput(binding.conversation);
  if (!status && Accepted(result))
    history_.FailAudioInput(binding.conversation);
  binding.settled = true;
  return status
             ? ObjectReply{}
             : Error(503, "realtime_input_retention_failed", status.message());
}
void RealtimeConversationInput::Discard(
    const RealtimeTranscriptionJob& job) noexcept {
  const auto found = bindings_.find(job.id);
  if (found == bindings_.end() || found->second.input != job.transcript) return;
  // A pipeline abort already settled committed input as failed. Keep its
  // retrievable item/audio; it must not disappear after a delayed join.
  if (found->second.settled && found->second.committed) return;
  try {
    history_.Erase(found->second.conversation);
  } catch (...) {
  }
  ReleaseNotice(found->second);
  bindings_.erase(found);
}
bool RealtimeConversationInput::Speech(bool started, std::string_view id,
                                       uint64_t ticks) {
  if (started) CancelIdle();
  return callbacks_.speech(started, id, ticks,
                           started && settings_.InterruptOnSpeechStart());
}
bool RealtimeConversationInput::Ready(const RealtimeTranscriptionJob&) {
  return !failed_ && !cancelled_ && callbacks_.ready();
}
void RealtimeConversationInput::ReleaseNotice(Binding& binding) {
  if (binding.captured) {
    --captures_;
    binding.captured = false;
  }
  binding.notice.reset();
}

bool RealtimeConversationInput::Drain() {
  for (size_t i = 0; i < 64; ++i) {
    const auto frame = output_.Front();
    if (frame.empty()) return true;
    size_t consumed = 0;
    if (decoder_.Consume(
            {reinterpret_cast<const uint8_t*>(frame.data()), frame.size()},
            &consumed) != WebSocketRead::kEvent ||
        consumed != frame.size())
      return false;
    Json event;
    if (!io::ParseJson(std::string(decoder_.Event().payload), &event, true,
                       {64, 65536, kWebSocketMaximumLimit, true}))
      return false;
    const bool forwarded = Forward(std::move(event));
    decoder_.Acknowledge();
    output_.Acknowledge();
    if (!forwarded) return false;
  }
  return true;
}
bool RealtimeConversationInput::Forward(Json event) {
  const auto type = event.GetString("type");
  if (type == "input_audio_buffer.cleared") CancelIdle();
  if (type == "session.created" || type == "session.updated") return true;
  if (type == "conversation.item.created") {
    const auto* item = event.Find("item");
    const auto found =
        item ? bindings_.find(item->GetString("id")) : bindings_.end();
    if (found == bindings_.end() || found->second.published) return true;
    auto& binding = found->second;
    Json public_item;
    if (!binding.committed || !history_.Get(binding.conversation, &public_item))
      return false;
    Set(&event, "item", std::move(public_item));
    Set(&event, "type", String("conversation.item.added"));
    if (!Accepted(callbacks_.emit(event))) return false;
    Set(&event, "type", String("conversation.item.done"));
    Set(&event, "event_id", String(detail::NewRequestId("event_")));
    if (!Accepted(callbacks_.emit(event))) return false;
    binding.published = true;
    if (!binding.notice) return false;
    auto notice = std::move(*binding.notice);
    ReleaseNotice(binding);
    const auto reply = callbacks_.committed(std::move(notice));
    return Accepted(reply) || ErrorEvent(reply);
  }
  if (type.starts_with("conversation.item.input_audio_transcription.")) {
    const auto found = bindings_.find(event.GetString("item_id"));
    if (found == bindings_.end()) return true;  // Deleted/abandoned generation.
    const auto& binding = found->second;
    const bool failed = type.ends_with(".failed");
    const bool terminal = failed || type.ends_with(".completed");
    bool sent = true;
    if (binding.settings.transcript_events)
      sent = Accepted(callbacks_.emit(event));
    else if (failed) {
      auto error = Event("error");
      Set(&error, "error", *event.Find("error"));
      sent = Accepted(callbacks_.emit(error));
    }
    if (terminal && !binding.committed) {
      ReleaseNotice(found->second);
      bindings_.erase(found);
    }
    return sent;
  }
  return Accepted(callbacks_.emit(event));
}
bool RealtimeConversationInput::Owns(const RealtimeItemTicket& ticket) const {
  const auto found = bindings_.find(ticket.id);
  return found != bindings_.end() && found->second.conversation == ticket &&
         found->second.committed;
}
ObjectReply RealtimeConversationInput::Retrieve(
    const RealtimeItemTicket& ticket, Json* item) const {
  if (!item || !Owns(ticket)) return Missing();
  const auto& binding = bindings_.find(ticket.id)->second;
  Json copy;
  if (!history_.Get(ticket, &copy)) return Missing();
  std::string encoded, base64;
  Status status;
  if (!binding.audio->Samples().empty())
    status = EncodeRealtimeAudio(binding.audio->Samples(), retrieval_format_,
                                 600ULL * 24000 * 2, &encoded, interrupted_);
  if (status)
    status = io::EncodeBase64(encoded, kWebSocketMaximumLimit, &base64);
  if (!status)
    return Error(503, "realtime_audio_retrieval_failed", status.message());
  Set(&Field(copy, "content").array[0], "audio", String(std::move(base64)));
  *item = std::move(copy);
  return {};
}
ObjectReply RealtimeConversationInput::Erase(const RealtimeItemTicket& ticket) {
  if (!Owns(ticket)) return Missing();
  const auto found = bindings_.find(ticket.id);
  auto reply = processor_.EraseInput(found->second.input);
  if (!Accepted(reply)) return reply;
  const auto status = history_.Erase(ticket);
  if (!status)
    return Error(500, "realtime_input_history_invalid", status.message());
  ReleaseNotice(found->second);
  bindings_.erase(found);
  return {};
}
}  // namespace q4t::server
