#include "q4t/server/live_audio_input.h"

#include <algorithm>

#include "object_protocol.h"
#include "q4t/io/base64.h"
#include "q4t/server/asr_response.h"
#include "q4t/server/lifetime.h"

namespace q4t::server {
namespace {
using namespace object_detail;
bool Accepted(const ObjectReply& reply) {
  return reply.ok() && !reply.commit_unknown && reply.http_status >= 200 &&
         reply.http_status < 300;
}
ObjectReply Unavailable() {
  return Error(503, "live_input_unavailable", "Native Live input unavailable");
}
ObjectReply Invalid(std::string_view message) {
  auto reply = Error(400, "invalid_audio", std::string(message));
  reply.value = Object({{"param", String("audio")}});
  return reply;
}
}  // namespace

LiveAudioInput::LiveAudioInput(std::string id, RealtimeConversation& history,
                               RealtimeTranscriptionCallbacks native,
                               LiveAudioInputCallbacks callbacks,
                               uint32_t max_audio_seconds, size_t message_bytes)
    : callbacks_(std::move(callbacks)),
      message_bytes_(message_bytes),
      input_(std::move(id), history, std::move(native), Bind(),
             max_audio_seconds, message_bytes) {}
LiveAudioInput::~LiveAudioInput() {
  Cancel();
  Join();
}
RealtimeConversationInputCallbacks LiveAudioInput::Bind() {
  RealtimeConversationInputCallbacks result;
  result.emit = [this](const Json& event) { return Forward(event); };
  result.ready = [this] {
    return !cancelled_ && !failed_ && callbacks_.ready();
  };
  result.speech = [this](bool started, auto id, auto ticks, bool interrupt) {
    return callbacks_.speech(started, id, ticks, interrupt);
  };
  result.committed = [this](RealtimeInputCommit notice) {
    return callbacks_.committed(std::move(notice));
  };
  result.capture_response = [this](RealtimePromptSelection* selected) {
    return callbacks_.capture_response(selected);
  };
  result.transcript = [this](const auto& event, const auto& audio) {
    return Transcript(event, audio);
  };
  return result;
}
ObjectReply LiveAudioInput::Prepare(RealtimeAudioFormat format) {
  if (prepared_ || closing_ || cancelled_ || failed_ || !callbacks_.emit ||
      !callbacks_.ready || !callbacks_.speech || !callbacks_.committed ||
      !callbacks_.capture_response || !message_bytes_ ||
      message_bytes_ > kWebSocketMessageLimit)
    return Unavailable();
  using E = RealtimeAudioEncoding;
  if (format.encoding == E::kPcm16
          ? format.rate != 16000 && format.rate != 24000
          : (format.encoding != E::kMuLaw && format.encoding != E::kALaw) ||
                format.rate != 8000)
    return Invalid("Invalid Live audio format");
  RealtimeDuplexInput settings;
  settings.enabled = true;
  settings.transcript_events = true;
  settings.processing.format = format;
  settings.processing.turn = RealtimeTranscriptionSettings::Turn::kServerVad;
  settings.processing.request.chunking.mode = AsrChunking::Mode::kServerVad;
  auto reply = input_.Configure(std::move(settings));
  if (!Accepted(reply)) return reply;
  format_ = format;
  prepared_ = true;
  return {};
}
ObjectReply LiveAudioInput::Fail() {
  failed_ = true;
  input_.Cancel();
  return Unavailable();
}
ObjectReply LiveAudioInput::ErrorEvent(ObjectReply reply,
                                       std::string_view event_id) {
  auto error = Object(
      {{"type", String(reply.http_status >= 500 ? "server_error"
                                                : "invalid_request_error")},
       {"code", String(std::string(reply.ClientCode()))},
       {"message", String(reply.ClientMessage())}});
  if (!event_id.empty())
    Set(&error, "client_event_id", String(std::string(event_id)));
  if (const auto* param = reply.value.Find("param"))
    Set(&error, "param", *param);
  auto result = callbacks_.emit(
      Object({{"type", String("error")}, {"error", std::move(error)}}));
  return Accepted(result) ? ObjectReply{} : Fail();
}
ObjectReply LiveAudioInput::Forward(const Json& event) {
  // Input item/history changes have their original native owner. Only Live's
  // transcript and error contracts are published on this connection.
  if (event.GetString("type") != "error") return {};
  const auto* error = event.Find("error");
  if (!error || !error->IsObject()) return Fail();
  auto reply = Error(error->GetString("type") == "server_error" ? 503 : 400,
                     error->GetString("code"), error->GetString("message"));
  if (const auto* param = error->Find("param"))
    reply.value = Object({{"param", *param}});
  if (in_command_) {
    command_error_ = std::move(reply);
    return {};
  }
  return ErrorEvent(std::move(reply), error->GetString("event_id"));
}
ObjectReply LiveAudioInput::Transcript(const Json& event,
                                       const RealtimeAudioItem& audio) {
  const auto type = event.GetString("type");
  const auto id = event.GetString("item_id");
  if (audio.end_ticks < audio.begin_ticks ||
      audio.end_ticks > kRealtimeSessionTicks)
    return Fail();
  if (id != transcript_id_) {
    transcript_id_ = id;
    transcript_text_.clear();
  }
  if (type.ends_with(".failed")) {
    const auto* error = event.Find("error");
    if (!error) return Fail();
    return ErrorEvent(
        Error(503, error->GetString("code"), error->GetString("message")));
  }
  std::string delta;
  if (type.ends_with(".delta")) {
    delta = event.GetString("delta");
  } else if (type.ends_with(".completed")) {
    const auto full = event.GetString("transcript");
    if (!full.starts_with(transcript_text_))
      return ErrorEvent(Error(502, "live_transcript_conflict",
                              "Final transcript differs from emitted prefix"));
    delta = full.substr(transcript_text_.size());
  } else {
    return Fail();
  }
  if (delta.size() > kAsrTranscriptBytes - transcript_text_.size())
    return Fail();
  if (delta.empty()) return {};
  auto result = callbacks_.emit(
      Object({{"type", String("session.input_transcript.delta")},
              {"delta", String(delta)},
              {"start_ms", Number(audio.begin_ticks / 48)},
              {"end_ms", Number(audio.end_ticks / 48)}}));
  if (!Accepted(result)) return Fail();
  transcript_text_ += delta;
  return {};
}
ObjectReply LiveAudioInput::Command(Json event) {
  command_error_ = {};
  in_command_ = true;
  ScopeExit restore([&] { in_command_ = false; });
  if (!input_.Message(OutputJson(event)) || input_.Failed()) return Fail();
  return command_error_;
}
ObjectReply LiveAudioInput::Append(std::string_view base64,
                                   std::string_view event_id) {
  if (!prepared_ || closing_ || cancelled_ || failed_) return Unavailable();
  if (base64.size() > message_bytes_)
    return Invalid("Audio exceeds the message budget");
  std::string raw;
  auto reply = DecodeRealtimeAudioBase64(base64, &raw);
  if (!Accepted(reply) || raw.empty())
    return Invalid("Invalid raw audio base64");
  std::string samples;
  bool partial = false;
  uint8_t byte = 0;
  if (format_.encoding == RealtimeAudioEncoding::kPcm16) {
    samples.reserve(raw.size() + size_t(partial_));
    size_t at = 0;
    if (partial_) {
      samples += partial_muted_ ? char(0) : char(pending_byte_);
      samples += partial_muted_ ? char(0) : raw[at];
      ++at;
    }
    const auto count = (raw.size() - at) / 2 * 2;
    if (muted_)
      samples.append(count, '\0');
    else
      samples.append(raw, at, count);
    at += count;
    if (at != raw.size()) {
      partial = true;
      byte = uint8_t(raw[at]);
    }
  } else {
    samples = std::move(raw);
    if (muted_)
      std::fill(samples.begin(), samples.end(),
                format_.encoding == RealtimeAudioEncoding::kMuLaw ? char(0xff)
                                                                  : char(0xd5));
  }
  if (!samples.empty()) {
    std::string encoded;
    if (!io::EncodeBase64(samples, message_bytes_, &encoded))
      return Invalid("Audio exceeds the message budget");
    auto event = Object({{"type", String("input_audio_buffer.append")},
                         {"audio", String(std::move(encoded))}});
    if (!event_id.empty())
      Set(&event, "event_id", String(std::string(event_id)));
    const auto begin = input_.AcceptedTicks();
    reply = Command(std::move(event));
    if (!Accepted(reply)) return reply;
    if (callbacks_.record)
      callbacks_.record(LiveAudioChannel::kInput, begin, samples);
  }
  partial_ = partial;
  partial_muted_ = muted_;
  pending_byte_ = byte;
  return {};
}
ObjectReply LiveAudioInput::Mute(bool muted, std::string_view event_id) {
  if (!prepared_ || closing_ || cancelled_ || failed_) return Unavailable();
  auto event =
      Object({{"type", String(muted ? "session.input_audio.muted"
                                    : "session.input_audio.unmuted")}});
  if (!event_id.empty())
    Set(&event, "client_event_id", String(std::string(event_id)));
  muted_ = muted;
  auto reply = callbacks_.emit(std::move(event));
  return Accepted(reply) ? ObjectReply{} : Fail();
}
ObjectReply LiveAudioInput::DiscardPending() {
  if (!prepared_ || closing_ || cancelled_ || failed_) return Unavailable();
  auto reply = Command(Object({{"type", String("input_audio_buffer.clear")}}));
  if (Accepted(reply)) partial_ = false;
  return reply;
}
ObjectReply LiveAudioInput::Tick(bool allow_work) {
  if (!prepared_ || cancelled_ || failed_) return Unavailable();
  if (!input_.Tick(allow_work) || input_.Failed()) return Fail();
  return closing_ && allow_work ? FinishTail() : ObjectReply{};
}
ObjectReply LiveAudioInput::Close() {
  if (!prepared_ || cancelled_ || failed_) return Unavailable();
  if (closing_) return {};
  if (partial_) return Invalid("Input ended within a PCM sample");
  closing_ = true;
  return FinishTail();
}
ObjectReply LiveAudioInput::FinishTail() {
  if (input_.UncommittedBytes()) {
    const size_t width =
        format_.encoding == RealtimeAudioEncoding::kPcm16 ? 2 : 1;
    // A whole 24k sample can still be shorter than one normalized ASR sample.
    // Close discards only that fractional tail, retaining its received clock;
    // it cannot pad an ASR waveform or invent a successful transcription.
    const bool fractional =
        uint64_t(input_.UncommittedBytes() / width) * 16000 / format_.rate == 0;
    const auto tail = input_.UncommittedTail();
    if (!fractional && tail == RealtimeInputTail::kAnalyzing) return {};
    const bool clear = fractional || tail == RealtimeInputTail::kSilence;
    auto reply = Command(
        Object({{"type", String(clear ? "input_audio_buffer.clear"
                                     : "input_audio_buffer.commit")}}));
    if (!Accepted(reply)) {
      failed_ = true;
      return reply;
    }
  }
  return {};
}
bool LiveAudioInput::Drained() const {
  return prepared_ && closing_ && !cancelled_ && !failed_ && !partial_ &&
         input_.Drained();
}
void LiveAudioInput::Cancel() noexcept {
  cancelled_ = true;
  input_.Cancel();
}
void LiveAudioInput::Join() noexcept { input_.Join(); }
}  // namespace q4t::server
