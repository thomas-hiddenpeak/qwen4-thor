#include "realtime_media_call.h"

#include <algorithm>

#include "object_protocol.h"
#include "q4t/io/base64.h"
#include "q4t/server/http_response.h"
#include "q4t/server/realtime_duplex_input.h"
#include "q4t/server/realtime_shape.h"

namespace q4t::server {
namespace {
using namespace object_detail;
using namespace std::chrono_literals;
constexpr size_t kPcmBytes = 8ULL << 20;
constexpr size_t kPacket = 960;  // 20 ms of 24 kHz, mono PCM16.
}  // namespace

RealtimeMediaCall::RealtimeMediaCall(Json defaults,
                                     RealtimeConversationCallbacks cb,
                                     size_t message_bytes, Send send,
                                     std::shared_ptr<LiveSideband> sideband,
                                     RealtimeCarrier carrier)
    : send_(std::move(send)),
      sideband_(std::move(sideband)),
      carrier_(carrier),
      channel_(carrier == RealtimeCarrier::kWebRtcData),
      conversation_(std::make_unique<RealtimeConversationSession>(
          detail::NewRequestId("sess_"), detail::NewRequestId("conv_"),
          std::move(defaults),
          SessionEventOutput{kWebSocketMaximumLimit, [this] { return failed_ || ended_; },
           [this](auto text) { return Publish(text); }},
          [&] {
            cb.interrupt_playout = [this] {
              if (!Clear(true)) failed_ = true;
            };
            return std::move(cb);
          }(),
          message_bytes)) {}

RealtimeMediaCall::Transcription::Transcription(
    RealtimeTranscriptionCallbacks callbacks, uint32_t max_seconds,
    size_t message_bytes)
    : session(detail::NewRequestId("sess_"), output, std::move(callbacks),
              max_seconds, message_bytes) {}

RealtimeMediaCall::RealtimeMediaCall(RealtimeTranscriptionCallbacks callbacks,
                                     uint32_t max_seconds, size_t message_bytes,
                                     Send send, std::shared_ptr<LiveSideband> sideband,
                                     bool data_channel)
    : send_(std::move(send)),
      sideband_(std::move(sideband)),
      carrier_(data_channel ? RealtimeCarrier::kWebRtcData : RealtimeCarrier::kWebRtcAudio),
      channel_(data_channel),
      transcription_(std::make_unique<Transcription>(std::move(callbacks), max_seconds,
                                                     message_bytes)) {}

ObjectReply RealtimeMediaCall::Start(const Json& configuration) {
  if (!send_ || (carrier_ == RealtimeCarrier::kSip && !sideband_))
    return Error(503, "realtime_media_unavailable",
                 "Call transport unavailable");
  if (transcription_) {
    auto reply = transcription_->session.StartConfigured(configuration);
    input_format_ = transcription_->session.InputFormat();
    if (reply.ok() && !DrainTranscription())
      return Error(503, "realtime_media_unavailable", "Session events unavailable");
    return reply;
  }
  return conversation_->StartCall(configuration, carrier_ == RealtimeCarrier::kSip
                                               ? RealtimeShape::kCallAccept
                                               : RealtimeShape::kSession);
}

void RealtimeMediaCall::PublishEvent(std::string_view wire,
                                     std::string_view session) {
  channel_.Queue(wire);
  if (sideband_) sideband_->Publish(wire, Clock::now(), session);
}

void RealtimeMediaCall::BufferEvent(const char* type, const std::string& id) {
  PublishEvent(OutputJson(
      Object({{"type", String(type)},
              {"response_id", String(id)},
              {"event_id", String(detail::NewRequestId("event_"))}})));
}

bool RealtimeMediaCall::Clear(bool default_only) {
  for (auto& speech : speech_) {
    if (speech.cleared || (default_only && !speech.in_conversation)) continue;
    speech.cleared = true;
    size_t queued = 0;
    for (const auto& chunk : speech.chunks) queued += chunk.size();
    pcm_bytes_ -= queued - speech.offset;
    speech.chunks.clear();
    speech.offset = 0;
    BufferEvent("output_audio_buffer.cleared", speech.id);
  }
  return TruncateCleared();
}

bool RealtimeMediaCall::TruncateCleared() {
  for (auto& speech : speech_) {
    if (!speech.cleared || speech.history_settled || speech.item_id.empty())
      continue;
    if (!conversation_->TruncatePlayout(speech.item_id, speech.submitted_samples))
      return false;
    speech.history_settled = true;
  }
  return true;
}

bool RealtimeMediaCall::Publish(std::string_view wire) {
  Json event;
  const auto bad = [this] {
    failed_ = true;
    return false;
  };
  if (!io::ParseJson(std::string(wire), &event, true)) return bad();
  const auto type = event.GetString("type");
  if (type == "session.created" || type == "session.updated") {
    const auto* settings = event.Find("session");
    RealtimeDuplexInput input;
    if (!settings || !PlanRealtimeDuplexInput(*settings, &input).ok())
      return bad();
    if (input_format_ != input.processing.format) encoder_.reset();
    input_format_ = input.processing.format;
    PublishEvent(wire, OutputJson(*settings));
    return true;
  }
  if (type == "input_audio_buffer.cleared") encoder_.reset();
  const auto* response = event.Find("response");
  if (type == "response.created" && response && response->Has("audio")) {
    const auto* output = response->Find("audio")->Find("output");
    const auto* format = output ? output->Find("format") : nullptr;
    if (!format) return bad();
    const auto codec = format->GetString("type");
    RealtimeAudioFormat from;
    if (codec == "audio/pcma" || codec == "audio/pcmu") {
      from = {codec == "audio/pcmu" ? RealtimeAudioEncoding::kMuLaw
                                    : RealtimeAudioEncoding::kALaw,
              8000};
    } else if (codec != "audio/pcm" || format->GetInt("rate") != 24000) {
      return bad();
    }
    if (speech_.size() >= 16) return false;
    speech_.push_back({.id = response->GetString("id"),
                       .decoder = {from, {}},
                       .chunks = {},
                       .item_id = {},
                       .in_conversation = response->Has("conversation_id")});
  } else if (type == "response.content_part.added") {
    const auto* part = event.Find("part");
    if (part && part->GetString("type") == "audio") {
      const auto it = std::ranges::find(speech_, event.GetString("response_id"),
                                        &Speech::id);
      if (it == speech_.end() || !it->item_id.empty()) return bad();
      it->item_id = event.GetString("item_id");
    }
  } else if (type == "response.output_audio.delta" || type == "response.done") {
    const bool done = type == "response.done";
    const auto id = done && response ? response->GetString("id")
                                     : event.GetString("response_id");
    auto it = std::ranges::find(speech_, id, &Speech::id);
    if (it == speech_.end()) {
      if (!done) return bad();
    } else {
      std::string bytes, pcm;
      if (!done &&
          !DecodeRealtimeAudioBase64(event.GetString("delta"), &bytes).ok())
        return bad();
      if (!it->cleared) {
        // Check capacity before advancing the streaming resampler. A deferred
        // session event may be offered again verbatim when capacity returns.
        if (bytes.size() * 6 + 1024 > kPcmBytes - pcm_bytes_ ||
            it->chunks.size() >= 4096)
          return false;
        if (!it->decoder.Convert(bytes, &pcm, done)) return bad();
        if (!pcm.empty()) {
          pcm_bytes_ += pcm.size();
          it->chunks.push_back(std::move(pcm));
        }
      }
      it->done = done;
    }
    if (!done) return true;  // Audio is delivered to the negotiated RTP peer.
  }
  PublishEvent(wire);
  return true;
}

bool RealtimeMediaCall::Input(std::string_view pcm, bool finish) {
  if (!encoder_) encoder_.emplace(RealtimeAudioFormat{}, input_format_);
  std::string bytes, encoded;
  if (!encoder_->Convert(pcm, &bytes, finish)) return false;
  if (finish) encoder_.reset();
  if (bytes.empty()) return true;
  if (!io::EncodeBase64(bytes, kWebSocketMessageLimit, &encoded)) return false;
  return Deliver(
      WebSocketOpcode::kText,
      OutputJson(Object({{"type", String("input_audio_buffer.append")},
                         {"audio", String(std::move(encoded))}})));
}

bool RealtimeMediaCall::DrainTranscription() {
  auto& asr = *transcription_;
  for (size_t count = 0; count < 64; ++count) {
    const auto frame = asr.output.Front();
    if (frame.empty()) break;
    size_t consumed = 0;
    if (asr.decoder.Consume({reinterpret_cast<const uint8_t*>(frame.data()), frame.size()},
                            &consumed) != WebSocketRead::kEvent ||
        consumed != frame.size() || asr.decoder.Event().opcode != WebSocketOpcode::kText)
      return false;
    const auto wire = asr.decoder.Event().payload;
    Json event;
    if (!io::ParseJson(std::string(wire), &event, true,
                       {64, 65536, kWebSocketMaximumLimit, true})) return false;
    const auto type = event.GetString("type");
    if (type == "transcription_session.updated")
      PublishEvent(wire, OutputJson(asr.session.SessionResource()));
    else if (const auto* settings = event.Find("session"))
      PublishEvent(wire, OutputJson(*settings));
    else
      PublishEvent(wire);
    asr.decoder.Acknowledge();
    asr.output.Acknowledge();
  }
  return true;
}

bool RealtimeMediaCall::Deliver(WebSocketOpcode opcode, std::string_view wire) {
  if (!transcription_) return conversation_->Message(opcode, wire);
  if (!transcription_->session.Message(opcode, wire)) return false;
  const auto format = transcription_->session.InputFormat();
  if (format != input_format_) encoder_.reset();
  input_format_ = format;
  return DrainTranscription();
}

bool RealtimeMediaCall::Receive(char type, std::string_view payload) {
  if (failed_ || ended_) return false;
  if (type == 'V' && payload.empty() && !verified_) return verified_ = true;
  if (!verified_) return false;
  if (type == 'R' || type == 'C' || type == 'D')
    return channel_.Receive(type, payload, [this](auto wire) {
      return Control(WebSocketOpcode::kText, wire);
    });
  if (type == 'K' && carrier_ == RealtimeCarrier::kSip && payload.size() == 1 &&
      std::string_view("0123456789*#ABCD").find(payload.front()) !=
          payload.npos) {
    const auto timestamp =
        std::chrono::duration_cast<std::chrono::seconds>(
            std::chrono::system_clock::now().time_since_epoch())
            .count();
    sideband_->Publish(
        OutputJson(
            Object({{"type", String("input_audio_buffer.dtmf_event_received")},
                    {"event", String(std::string(payload))},
                    {"received_at", Number(timestamp)}})),
        Clock::now());
    return true;
  }
  return type == 'P' && !payload.empty() && Input(payload);
}

bool RealtimeMediaCall::Control(WebSocketOpcode opcode, std::string_view wire) {
  Json event;
  if (opcode == WebSocketOpcode::kText &&
      io::ParseJson(std::string(wire), &event, true,
                    {64, 65536, kWebSocketMessageLimit, true})) {
    const auto type = event.GetString("type");
    if (event.GetString("event_id").size() > 512)
      return Deliver(opcode, wire);
    ChatPlanError error;
    if (conversation_ && type == "output_audio_buffer.clear" &&
        ValidateRealtimeWireShape(event, RealtimeShape::kClientEvent, &error)) {
      return Clear(false);
    }
    const auto* update = event.Find("session");
    const auto* audio = update ? update->Find("audio") : nullptr;
    const auto* input = audio ? audio->Find("input") : nullptr;
    const bool may_change_format =
        type == "session.update" && update &&
        (update->Has("prompt") || (input && input->Has("format")));
    bool flush = (type == "input_audio_buffer.commit" || may_change_format) &&
                 ValidateRealtimeWireShape(event, RealtimeShape::kClientEvent, &error);
    if (transcription_ && type == "transcription_session.update" && update &&
        update->Has("input_audio_format")) {
      RealtimeTranscriptionCommand command;
      flush = ParseRealtimeTranscriptionCommand(wire, kWebSocketMessageLimit,
                                                &command).ok();
    }
    if (encoder_ && flush && !Input({}, true)) return false;
  }
  return Deliver(opcode, wire);
}

bool RealtimeMediaCall::Playback(Clock::time_point now) {
  if (!verified_ || now < next_) return true;
  std::erase_if(speech_, [&](const auto& stream) {
    if (!stream.done || !stream.chunks.empty()) return false;
    if (stream.started && !stream.cleared)
      BufferEvent("output_audio_buffer.stopped", stream.id);
    return true;
  });
  // An out-of-band response may wait on a tool while a newer response speaks.
  const auto ready = std::ranges::find_if(
      speech_, [](const auto& stream) { return !stream.chunks.empty(); });
  if (ready != speech_.end()) {
    auto& speech = *ready;
    auto chunk =
        std::string_view(speech.chunks.front()).substr(speech.offset, kPacket);
    if (!send_('P', chunk)) {
      if (blocked_ == Clock::time_point{}) blocked_ = now;
      return now - blocked_ < 15s;
    }
    blocked_ = {};
    speech.submitted_samples += chunk.size() / 2;
    if (!speech.started) {
      speech.started = true;
      BufferEvent("output_audio_buffer.started", speech.id);
    }
    pcm_bytes_ -= chunk.size();
    speech.offset += chunk.size();
    if (speech.offset == speech.chunks.front().size()) {
      speech.chunks.pop_front();
      speech.offset = 0;
    }
    next_ = now + std::chrono::microseconds(chunk.size() * 1000000ULL / 48000);
  }
  return true;
}

bool RealtimeMediaCall::Tick(Clock::time_point now) {
  if (ended_) return true;
  if (auto command = sideband_ ? sideband_->Pop() : std::nullopt;
      command && !Control(command->opcode, command->text))
    return false;
  if (failed_) return false;
  if (transcription_) {
    if (!transcription_->session.Tick() || !DrainTranscription()) return false;
  } else if (!conversation_->Tick() || !TruncateCleared()) {
    return false;
  }
  if (verified_ && !channel_.Flush(now, send_)) {
    if (channel_blocked_ == Clock::time_point{}) channel_blocked_ = now;
    if (now - channel_blocked_ >= 15s) channel_.Disconnect();
  } else {
    channel_blocked_ = {};
  }
  return Playback(now);
}

bool RealtimeMediaCall::EndTransport() {
  ended_ = true;
  if (transcription_) {
    transcription_->session.Cancel();
    transcription_->session.Join();
  } else {
    conversation_->Cancel();
    conversation_->Join();
  }
  speech_.clear();
  return true;
}
}  // namespace q4t::server
