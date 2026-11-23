#include "q4t/server/live_audio_output.h"

#include "object_protocol.h"
#include "q4t/io/base64.h"

namespace q4t::server {
namespace {
using namespace object_detail;
constexpr uint64_t kSourceBytes = 600ULL * 24000 * 2;
bool Accepted(const ObjectReply& reply) {
  return reply.ok() && !reply.commit_unknown && reply.http_status >= 200 &&
         reply.http_status < 300;
}
}  // namespace
LiveAudioOutput::LiveAudioOutput(RealtimeAudioFormat format,
                                 uint64_t begin_ticks, LiveEventSink emit,
                                 std::function<bool()> cancelled,
                                 std::string client_event,
                                 LiveRecordSink record)
    : format_(format),
      begin_ticks_(begin_ticks),
      emit_(std::move(emit)),
      record_(std::move(record)),
      interrupted_(std::move(cancelled)),
      client_event_(std::move(client_event)),
      encoder_(format, kSourceBytes / 2, kRealtimeAudioBytes, [this] {
        return cancelled_ || (interrupted_ && interrupted_());
      }) {
  valid_ = emit_ && encoder_.Valid() && begin_ticks_ < kRealtimeSessionTicks &&
           client_event_.size() <= 256;
}
ObjectReply LiveAudioOutput::Fail(std::string_view message) {
  if (error_.ok())
    error_ = object_detail::Error(502, "live_audio_output_invalid",
                                  std::string(message));
  return error_;
}
ObjectReply LiveAudioOutput::Emit(Json event) {
  if (!Accepted(error_)) return error_;
  if (cancelled_ || (interrupted_ && interrupted_()))
    return Fail("Live audio delivery cancelled");
  auto reply = emit_(std::move(event));
  if (!Accepted(reply)) {
    if (reply.code.empty())
      return Fail("Live audio delivery was not confirmed");
    error_ = std::move(reply);
  }
  return error_;
}
ObjectReply LiveAudioOutput::Write(std::string_view bytes) {
  const size_t width =
      format_.encoding == RealtimeAudioEncoding::kPcm16 ? 2 : 1;
  if (bytes.empty() || bytes.size() % width ||
      bytes.size() > kRealtimeAudioBytes - output_bytes_)
    return Fail("Converted audio exceeds its sample or byte limit");
  const uint64_t ticks = bytes.size() / width * (48000 / format_.rate);
  if (ticks > kRealtimeSessionTicks - begin_ticks_ - output_ticks_)
    return Fail("Converted audio exceeds the session timeline");
  std::string encoded;
  if (!io::EncodeBase64(bytes, kWebSocketMessageLimit, &encoded))
    return Fail("Could not encode Live audio");
  auto reply = Emit(Object({{"type", String("session.output_audio.delta")},
                            {"delta", String(std::move(encoded))}}));
  if (!Accepted(reply)) return reply;
  if (record_) record_(LiveAudioChannel::kOutput, EndTicks(), bytes);
  output_bytes_ += bytes.size();
  output_ticks_ += ticks;
  return {};
}
ObjectReply LiveAudioOutput::Append(const RealtimeSpeechEvent& event) {
  if (!Accepted(error_)) return error_;
  if (!valid_ || completed_ || cancelled_ || (interrupted_ && interrupted_()))
    return Fail("Live speech owner is not active");
  try {
    if (event.kind == RealtimeSpeechEvent::Kind::kChunk) {
      if (event.bytes.empty() || !event.transcript.empty() ||
          event.bytes.size() > RealtimeSpeechEncoder::kMaximumChunkBytes ||
          event.bytes.size() > kSourceBytes - input_bytes_ ||
          event.receipt.bytes || event.receipt.input_samples ||
          event.receipt.output_samples)
        return Fail("Invalid native PCM fragment");
      // Normalized source is the original verified 24k bypass, never an
      // already converted Realtime G.711 stream. Partial bytes stay unpadded.
      auto reply = encoder_.Append(event.bytes,
                                   [&](auto bytes) { return Write(bytes); });
      if (!Accepted(reply)) {
        error_ = std::move(reply);
        return error_;
      }
      input_bytes_ += event.bytes.size();
      return {};
    }
    const auto& source = event.receipt;
    if (event.kind != RealtimeSpeechEvent::Kind::kDone ||
        !event.bytes.empty() || event.transcript.empty() ||
        event.transcript.size() > 1ULL << 20 || !input_bytes_ ||
        input_bytes_ % 2 || source.bytes != input_bytes_ ||
        source.output_samples != input_bytes_ / 2 ||
        source.input_samples != source.output_samples)
      return Fail("Native EOF does not match the actual 24k waveform");
    RealtimeSpeechReceipt converted;
    auto reply =
        encoder_.Finish([&](auto bytes) { return Write(bytes); }, &converted);
    if (!Accepted(reply)) {
      error_ = std::move(reply);
      return error_;
    }
    const size_t width =
        format_.encoding == RealtimeAudioEncoding::kPcm16 ? 2 : 1;
    if (!output_bytes_ || converted.input_samples != source.output_samples ||
        converted.output_samples != output_bytes_ / width ||
        converted.bytes != output_bytes_)
      return Fail("Live EOF does not match the delivered waveform");
    // This interval describes actual produced speech, not a listener receipt.
    // Do not publish the whole transcript before native synthesis succeeds.
    auto transcript =
        Object({{"type", String("session.output_transcript.delta")},
                {"delta", String(event.transcript)},
                {"start_ms", Number(begin_ticks_ / 48)},
                {"end_ms", Number(EndTicks() / 48)}});
    if (!client_event_.empty())
      Set(&transcript, "client_event_id", String(client_event_));
    reply = Emit(std::move(transcript));
    if (Accepted(reply)) completed_ = true;
    return reply;
  } catch (...) {
    return Fail("Live audio callback failed");
  }
}
}  // namespace q4t::server
