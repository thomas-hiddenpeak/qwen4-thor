#include "q4t/server/live_audio_fixture.h"

#include <algorithm>

#include "object_protocol.h"
#include "q4t/io/base64.h"
#include "q4t/server/realtime_transcription_protocol.h"

namespace q4t::server {
namespace {
using namespace object_detail;
bool Accepted(const ObjectReply& reply) {
  return reply.ok() && !reply.commit_unknown && reply.http_status >= 200 &&
         reply.http_status < 300;
}
ObjectReply Invalid(std::string message) {
  auto reply = Error(400, "invalid_audio", std::move(message));
  reply.value = Object({{"param", String("audio")}});
  return reply;
}
ObjectReply Inactive() {
  return Error(409, "live_fixture_inactive", "Audio fixture is not active");
}
}  // namespace

LiveAudioFixture::LiveAudioFixture(RealtimeAudioFormat format,
                                   LiveEventSink emit,
                                   std::function<Clock::time_point()> now,
                                   LiveRecordSink record)
    : format_(format),
      emit_(std::move(emit)),
      record_(std::move(record)),
      now_(std::move(now)),
      encoder_(format, kMaximumBytes / 2, kMaximumBytes,
               [this] { return cancelled_; }) {}
ObjectReply LiveAudioFixture::Prepare(std::string_view audio,
                                      std::string client_event) {
  if (prepared_ || accepted_ || cancelled_ || !emit_ || !encoder_.Valid())
    return Inactive();
  if (client_event.size() > 256)
    return Invalid("Fixture event ID exceeds its limit");
  if (audio.empty() || audio.size() > (kMaximumBytes + 2) / 3 * 4)
    return Invalid("Fixture requires complete PCM24 of at most 30 seconds");
  std::string pcm;
  auto reply = DecodeRealtimeAudioBase64(audio, &pcm);
  if (!Accepted(reply) || pcm.empty() || pcm.size() % 2 ||
      pcm.size() > kMaximumBytes)
    return Invalid("Fixture requires complete raw mono PCM16 at 24 kHz");
  if ((pcm.size() >= 12 && pcm.substr(0, 4) == "RIFF" &&
       pcm.substr(8, 4) == "WAVE") ||
      pcm.starts_with("OggS") || pcm.starts_with("fLaC") ||
      pcm.starts_with("ID3"))
    return Invalid("Fixture must not contain an audio container header");
  pcm_ = std::move(pcm);
  client_event_ = std::move(client_event);
  input_bytes_ = pcm_.size();
  prepared_ = true;
  return {};
}
ObjectReply LiveAudioFixture::Fail(ObjectReply reply) {
  if (Accepted(error_)) error_ = std::move(reply);
  cancelled_ = true;
  return error_;
}
ObjectReply LiveAudioFixture::Emit(Json event) {
  try {
    auto reply = emit_(std::move(event));
    if (Accepted(reply)) return {};
    reply.commit_unknown = true;
    return Fail(std::move(reply));
  } catch (...) {
    return Fail(Error(503, "live_fixture_sink_failed",
                      "Fixture publication was not confirmed", true));
  }
}
ObjectReply LiveAudioFixture::Accept() {
  if (!prepared_ || accepted_ || cancelled_) return Inactive();
  accepted_ = true;
  auto event =
      Object({{"type", String("session.output_audio.fixture.playing")}});
  if (!client_event_.empty())
    Set(&event, "client_event_id", String(client_event_));
  return Emit(std::move(event));
}
ObjectReply LiveAudioFixture::Write(std::string_view bytes) {
  const size_t width =
      format_.encoding == RealtimeAudioEncoding::kPcm16 ? 2 : 1;
  if (bytes.empty() || bytes.size() % width ||
      bytes.size() > kMaximumBytes - output_bytes_)
    return Fail(Invalid("Invalid converted fixture extent"));
  std::string encoded;
  if (!io::EncodeBase64(bytes, kWebSocketMessageLimit, &encoded))
    return Fail(Invalid("Could not encode fixture audio"));
  auto reply = Emit(Object({{"type", String("session.output_audio.delta")},
                            {"delta", String(std::move(encoded))}}));
  if (Accepted(reply)) {
    if (record_)
      record_(LiveAudioChannel::kOutput,
              begin_ticks_ + output_bytes_ / width * (48000 / format_.rate),
              bytes);
    output_bytes_ += bytes.size();
  }
  return reply;
}
ObjectReply LiveAudioFixture::Tick(uint64_t timeline) {
  if (!Accepted(error_)) return error_;
  if (completed_) return {};
  if (!accepted_ || cancelled_) return Inactive();
  try {
    const auto now = now_ ? now_() : Clock::now();
    if (!started_clock_) {
      if (timeline > kRealtimeSessionTicks ||
          input_bytes_ > kRealtimeSessionTicks - timeline)
        return Fail(Invalid("Fixture exceeds the session timeline"));
      started_clock_ = true;
      started_ = observed_ = now;
      begin_ticks_ = timeline;
    }
    if (now < observed_) return Fail(Invalid("Fixture clock moved backwards"));
    observed_ = now;
    // 24k S16LE bytes and 48k timeline ticks have the same count. Preserve
    // fractional converted samples in time; never pad the negotiated PCM.
    const auto due =
        started_ +
        std::chrono::nanoseconds((position_ * 1000000000ULL + 47999) / 48000);
    if (now < due) return {};
    const auto expected_samples = input_bytes_ / 2 * format_.rate / 24000;
    if (position_ < input_bytes_) {
      const auto count = std::min<size_t>(960, input_bytes_ - position_);
      if (expected_samples) {
        auto reply =
            encoder_.Append(std::string_view(pcm_).substr(position_, count),
                            [&](auto bytes) { return Write(bytes); });
        if (!Accepted(reply)) return Fail(std::move(reply));
      }
      position_ += count;
      return {};
    }
    RealtimeSpeechReceipt receipt{input_bytes_ / 2, 0, 0};
    if (expected_samples) {
      auto reply =
          encoder_.Finish([&](auto bytes) { return Write(bytes); }, &receipt);
      if (!Accepted(reply)) return Fail(std::move(reply));
    }
    const size_t width =
        format_.encoding == RealtimeAudioEncoding::kPcm16 ? 2 : 1;
    if (receipt.input_samples != input_bytes_ / 2 ||
        receipt.output_samples != expected_samples ||
        receipt.bytes != output_bytes_ ||
        output_bytes_ != expected_samples * width)
      return Fail(Invalid("Fixture drain does not match actual samples"));
    auto event =
        Object({{"type", String("session.output_audio.fixture.played")}});
    if (!client_event_.empty())
      Set(&event, "client_event_id", String(client_event_));
    auto reply = Emit(std::move(event));
    if (Accepted(reply)) {
      completed_ = true;
      std::string().swap(pcm_);
    }
    return reply;
  } catch (...) {
    return Fail(Error(503, "live_fixture_failed", "Fixture playback failed"));
  }
}
void LiveAudioFixture::Cancel() noexcept {
  cancelled_ = true;
  std::string().swap(pcm_);
}
}  // namespace q4t::server
