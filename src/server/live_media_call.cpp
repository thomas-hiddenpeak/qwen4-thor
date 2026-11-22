#include "q4t/server/live_media_call.h"

#include <algorithm>

#include "object_protocol.h"
#include "q4t/io/base64.h"
#include "q4t/server/realtime_transcription_protocol.h"

namespace q4t::server {
namespace {
using namespace object_detail;
constexpr size_t kPacketBytes = 65535, kPcmPacket = 960;
constexpr size_t kDataBytes = 4ULL << 20, kPcmBytes = 4ULL << 20;
}  // namespace

LiveMediaCall::LiveMediaCall(std::string id, uint64_t expires_at,
                             bool expect_channel,
                             LiveSessionCallbacks callbacks,
                             LiveSessionLimits limits, Send send, std::shared_ptr<LiveSideband> sideband)
    : send_(std::move(send)),
      sideband_(std::move(sideband)),
      expect_channel_(expect_channel),
      session_(std::move(id), expires_at,
               {kWebSocketMaximumLimit, [this] { return failed_; },
                [this](auto text) { return Publish(text); }},
               std::move(callbacks), limits) {}

ObjectReply LiveMediaCall::Start(const Json& configuration) {
  ChatPlanError error;
  LiveMediaPolicy permitted;
  const auto status = permitted.Configure(configuration.Find("client"), &error);
  if (!status) {
    auto reply = Error(error.http_status, error.code, status.message());
    reply.value = Object({{"param", String(error.param)}});
    return reply;
  }
  if (!send_ || !session_.Start())
    return Error(503, "live_session_unavailable",
                 "Media session could not start");
  frontend_ = std::move(permitted);
  return session_.StartMedia(configuration);
}

bool LiveMediaCall::Publish(std::string_view wire) {
  Json event;
  if (!io::ParseJson(std::string(wire), &event, true)) {
    failed_ = true;
    return false;
  }
  if (event.GetString("type") == "session.output_audio.delta") {
    const auto* delta = event.Find("delta");
    std::string pcm;
    if (!delta || !delta->IsString() ||
        !DecodeRealtimeAudioBase64(delta->str, &pcm).ok() || pcm.empty() ||
        pcm.size() % 2 || pcm.size() > kPcmBytes) {
      failed_ = true;
      return false;
    }
    if (pcm.size() > kPcmBytes - queued_pcm_ || media_.size() >= 512)
      return false;
    queued_pcm_ += pcm.size();
    media_.push_back(std::move(pcm));
  } else {
    if (sideband_) sideband_->Publish(wire, Clock::now());
    if (!expect_channel_ || closed_ || !frontend_.AllowsServer(event)) return true;
    if (wire.size() > kPacketBytes || wire.size() > kDataBytes - queued_data_ ||
        data_.size() >= 512) {
      DropData();
      close_pending_ = true;
      return true;
    }
    data_.emplace_back(wire);
    queued_data_ += wire.size();
  }
  return true;
}

void LiveMediaCall::DropData() {
  closed_ = true;
  data_.clear();
  queued_data_ = 0;
}

bool LiveMediaCall::Receive(char type, std::string_view payload) {
  if (failed_ || payload.size() > kPacketBytes) return false;
  if (type == 'V' && payload.empty() && !verified_) {
    verified_ = true;
    return true;
  }
  if (!verified_) return false;
  if (type == 'R' && expect_channel_ && !opened_ && payload.empty()) {
    opened_ = true;
    return true;
  }
  if (type == 'C' && expect_channel_ && payload.empty()) {
    close_pending_ = false;
    DropData();
    return true;
  }
  if (type == 'D' && opened_ && closed_) return true;
  if (type == 'D' && opened_)
    return session_.Message(WebSocketOpcode::kText, payload, &frontend_);
  if (type != 'P' || payload.empty() || payload.size() % 2) return false;
  std::string encoded;
  if (!io::EncodeBase64(payload, kWebSocketMessageLimit, &encoded))
    return false;
  if (sideband_) sideband_->InputAudio(encoded);
  return session_.Message(
      WebSocketOpcode::kText,
      OutputJson(Object({{"type", String("session.input_audio.append")},
                         {"audio", String(std::move(encoded))}})));
}

bool LiveMediaCall::Tick(Clock::time_point now) {
  if (failed_) return false;
  if (sideband_) {
    if (auto command = sideband_->Pop();
        command && !session_.SidebandMessage(command->opcode, command->text))
      return false;
  }
  if (!session_.Tick()) return false;
  bool blocked = false;
  if (verified_ && !opened_ && !closed_ && !data_.empty()) {
    if (waiting_data_ == Clock::time_point{}) waiting_data_ = now;
    if (now - waiting_data_ >= std::chrono::seconds(15)) {
      DropData();
      close_pending_ = true;
    }
  }
  if (opened_ && close_pending_ && send_('C', {})) close_pending_ = false;
  if (opened_ && !closed_) {
    for (int i = 0; i < 16 && !data_.empty(); ++i) {
      if (!send_('D', data_.front())) {
        blocked = true;
        break;
      }
      queued_data_ -= data_.front().size();
      data_.pop_front();
    }
  }
  if (verified_ && !media_.empty() && now >= next_audio_) {
    auto chunk =
        std::string_view(media_.front()).substr(pcm_offset_, kPcmPacket);
    if (send_('P', chunk)) {
      pcm_offset_ += chunk.size();
      queued_pcm_ -= chunk.size();
      if (pcm_offset_ == media_.front().size()) {
        pcm_offset_ = 0;
        media_.pop_front();
      }
      // Use the real playback clock. Never burst accumulated audio after a
      // stall.
      next_audio_ =
          now + std::chrono::microseconds(chunk.size() * 1000000ULL / 48000);
    } else {
      blocked = true;
    }
  }
  if (!blocked)
    blocked_since_ = {};
  else if (blocked_since_ == Clock::time_point{})
    blocked_since_ = now;
  return !blocked || now - blocked_since_ < std::chrono::seconds(15);
}

bool LiveMediaCall::Finished() const {
  return session_.Finalized() && data_.empty() && media_.empty();
}
}  // namespace q4t::server
