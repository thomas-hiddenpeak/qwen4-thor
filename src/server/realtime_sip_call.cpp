#include "realtime_sip_call.h"

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

RealtimeSipCall::RealtimeSipCall(Json defaults,
                                 RealtimeConversationCallbacks cb,
                                 size_t message_bytes, Send send,
                                 std::shared_ptr<LiveSideband> sideband)
    : send_(std::move(send)),
      sideband_(std::move(sideband)),
      session_(
          detail::NewRequestId("sess_"), detail::NewRequestId("conv_"),
          std::move(defaults),
          {kWebSocketMaximumLimit, [this] { return failed_ || ended_; },
           [this](auto text) { return Publish(text); }},
          [&] {
            cb.interrupt_playout = [this] { Clear(); };
            return std::move(cb);
          }(),
          message_bytes) {}

ObjectReply RealtimeSipCall::Start(const Json& configuration) {
  if (!send_ || !sideband_)
    return Error(503, "realtime_media_unavailable",
                 "Call transport unavailable");
  return session_.StartCall(configuration);
}

void RealtimeSipCall::BufferEvent(const char* type, const std::string& id) {
  sideband_->Publish(
      OutputJson(
          Object({{"type", String(type)},
                  {"response_id", String(id)},
                  {"event_id", String(detail::NewRequestId("event_"))}})),
      Clock::now());
}

void RealtimeSipCall::Clear() {
  for (auto& speech : speech_) {
    if (speech.cleared) continue;
    speech.cleared = true;
    speech.chunks.clear();
    speech.offset = 0;
    BufferEvent("output_audio_buffer.cleared", speech.id);
  }
  pcm_bytes_ = 0;
}

bool RealtimeSipCall::Publish(std::string_view wire) {
  Json event;
  const auto bad = [this] {
    failed_ = true;
    return false;
  };
  if (!io::ParseJson(std::string(wire), &event, true)) return bad();
  const auto type = event.GetString("type");
  const auto now = Clock::now();
  if (type == "session.created" || type == "session.updated") {
    const auto* settings = event.Find("session");
    RealtimeDuplexInput input;
    if (!settings || !PlanRealtimeDuplexInput(*settings, &input).ok())
      return bad();
    if (input_format_ != input.processing.format) encoder_.reset();
    input_format_ = input.processing.format;
    sideband_->Publish(wire, now, OutputJson(*settings));
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
    speech_.push_back({response->GetString("id"), {from, {}}, {}});
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
  sideband_->Publish(wire, now);
  return true;
}

bool RealtimeSipCall::Input(std::string_view pcm, bool finish) {
  if (!encoder_) encoder_.emplace(RealtimeAudioFormat{}, input_format_);
  std::string bytes, encoded;
  if (!encoder_->Convert(pcm, &bytes, finish)) return false;
  if (finish) encoder_.reset();
  if (bytes.empty()) return true;
  if (!io::EncodeBase64(bytes, kWebSocketMessageLimit, &encoded)) return false;
  return session_.Message(
      WebSocketOpcode::kText,
      OutputJson(Object({{"type", String("input_audio_buffer.append")},
                         {"audio", String(std::move(encoded))}})));
}

bool RealtimeSipCall::Receive(char type, std::string_view payload) {
  if (failed_ || ended_) return false;
  if (type == 'V' && payload.empty() && !verified_) return verified_ = true;
  if (!verified_) return false;
  if (type == 'K' && payload.size() == 1 &&
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

bool RealtimeSipCall::Control(WebSocketOpcode opcode, std::string_view wire) {
  Json event;
  if (opcode == WebSocketOpcode::kText &&
      io::ParseJson(std::string(wire), &event, true,
                    {64, 65536, kWebSocketMessageLimit, true})) {
    const auto type = event.GetString("type");
    if (event.GetString("event_id").size() > 512)
      return session_.Message(opcode, wire);
    ChatPlanError error;
    if (type == "output_audio_buffer.clear" &&
        ValidateRealtimeWireShape(event, RealtimeShape::kClientEvent, &error)) {
      Clear();
      return true;
    }
    const auto* update = event.Find("session");
    const auto* audio = update ? update->Find("audio") : nullptr;
    const auto* input = audio ? audio->Find("input") : nullptr;
    const bool may_change_format =
        type == "session.update" && update &&
        (update->Has("prompt") || (input && input->Has("format")));
    if (encoder_ &&
        (type == "input_audio_buffer.commit" || may_change_format) &&
        ValidateRealtimeWireShape(event, RealtimeShape::kClientEvent, &error) &&
        !Input({}, true))
      return false;
  }
  return session_.Message(opcode, wire);
}

bool RealtimeSipCall::Playback(Clock::time_point now) {
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

bool RealtimeSipCall::Tick(Clock::time_point now) {
  if (ended_) return true;
  if (auto command = sideband_->Pop();
      command && !Control(command->opcode, command->text))
    return false;
  return !failed_ && session_.Tick() && Playback(now);
}

bool RealtimeSipCall::EndTransport() {
  ended_ = true;
  session_.Cancel();
  session_.Join();
  speech_.clear();
  return true;
}
}  // namespace q4t::server
