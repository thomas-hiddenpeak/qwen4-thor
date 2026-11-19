#include "q4t/server/realtime_speech_encoder.h"

#include <algorithm>
#include <array>
#include <cmath>

#include "object_protocol.h"
#include "q4t/server/realtime_audio_export.h"
#include "q4t/server/realtime_pcm.h"

namespace q4t::server {
RealtimeSpeechEncoder::RealtimeSpeechEncoder(RealtimeAudioFormat format,
                                             size_t maximum_samples,
                                             size_t maximum_bytes,
                                             std::function<bool()> cancelled)
    : format_(format),
      maximum_samples_(maximum_samples),
      maximum_bytes_(maximum_bytes),
      cancelled_(std::move(cancelled)),
      resampler_(24000, maximum_samples, format.rate, false) {
  const bool pcm = format.encoding == RealtimeAudioEncoding::kPcm16;
  const bool g711 = format.encoding == RealtimeAudioEncoding::kMuLaw ||
                    format.encoding == RealtimeAudioEncoding::kALaw;
  valid_ = (pcm ? format.rate == 24000 : g711 && format.rate == 8000) &&
           maximum_samples && maximum_samples <= 600ULL * 24000 &&
           maximum_bytes && maximum_bytes <= kRealtimeAudioBytes &&
           resampler_.Valid();
}

ObjectReply RealtimeSpeechEncoder::Fail(std::string_view code,
                                        std::string_view message) {
  if (error_.ok())
    error_ = object_detail::Error(502, std::string(code), std::string(message));
  return error_;
}

ObjectReply RealtimeSpeechEncoder::Check() {
  if (!error_.ok()) return error_;
  if (!valid_ || finished_)
    return Fail("realtime_audio_state_invalid", "Audio stream is not active");
  try {
    if (cancelled_ && cancelled_())
      return Fail("realtime_audio_cancelled", "Audio conversion cancelled");
  } catch (...) {
    return Fail("realtime_audio_cancel_failed", "Cancellation check failed");
  }
  return {};
}

ObjectReply RealtimeSpeechEncoder::Write(std::span<const float> samples,
                                         const Sink& sink) {
  auto reply = Check();
  if (!reply.ok() || samples.empty()) return reply;
  const bool pcm = format_.encoding == RealtimeAudioEncoding::kPcm16;
  const size_t width = pcm ? 2 : 1;
  if (samples.size() > kOutputChunkBytes / width ||
      samples.size() > (maximum_bytes_ - output_bytes_) / width)
    return Fail("realtime_audio_capacity", "Audio output exceeds its budget");
  std::string bytes;
  bytes.reserve(samples.size() * width);
  for (const float value : samples) {
    if (!std::isfinite(value))
      return Fail("realtime_audio_invalid", "Audio sample is not finite");
    // Preserve sinc overshoot until final quantization, as in audio export.
    const auto linear = int16_t(std::clamp<long>(
        std::lround(double(std::clamp(value, -1.f, 1.f)) * 32768), -32768,
        32767));
    if (pcm) {
      const auto word = uint16_t(linear);
      bytes.push_back(char(word & 255));
      bytes.push_back(char(word >> 8));
    } else {
      bytes.push_back(char(EncodeRealtimeG711(
          linear, format_.encoding == RealtimeAudioEncoding::kMuLaw)));
    }
  }
  try {
    reply = sink(bytes);
  } catch (...) {
    return Fail("realtime_audio_sink_failed", "Audio sink raised an error");
  }
  if (!reply.ok() || reply.commit_unknown || reply.http_status < 200 ||
      reply.http_status >= 300) {
    if (reply.code.empty()) {
      reply.code = "realtime_audio_sink_unconfirmed";
      reply.message = "Audio sink did not confirm acceptance";
      reply.http_status = 503;
    }
    error_ = std::move(reply);
    return error_;
  }
  output_bytes_ += bytes.size();
  return Check();
}

ObjectReply RealtimeSpeechEncoder::Append(std::string_view pcm,
                                          const Sink& sink) {
  auto reply = Check();
  if (!reply.ok()) return reply;
  if (!sink || pcm.empty() || pcm.size() > kMaximumChunkBytes ||
      pcm.size() > maximum_samples_ * 2 - input_bytes_)
    return Fail("realtime_audio_input_invalid", "Invalid bounded PCM chunk");
  const uint64_t next_bytes = input_bytes_ + pcm.size();
  const uint64_t next_samples = next_bytes / 2 * format_.rate / 24000;
  const size_t width =
      format_.encoding == RealtimeAudioEncoding::kPcm16 ? 2 : 1;
  if (next_samples > maximum_bytes_ / width)
    return Fail("realtime_audio_capacity", "Audio output exceeds its budget");
  input_bytes_ = next_bytes;
  std::array<float, 1024> decoded, converted;
  size_t cursor = 0;
  while (cursor < pcm.size()) {
    reply = Check();
    if (!reply.ok()) return reply;
    size_t count = 0;
    if (pending_) {
      decoded[count++] = DecodeRealtimeSample(RealtimeAudioEncoding::kPcm16,
                                              pending_byte_, pcm[cursor++]);
      pending_ = false;
    }
    while (cursor + 1 < pcm.size() && count < decoded.size()) {
      decoded[count++] = DecodeRealtimeSample(RealtimeAudioEncoding::kPcm16,
                                              pcm[cursor], pcm[cursor + 1]);
      cursor += 2;
    }
    if (cursor + 1 == pcm.size()) {
      pending_byte_ = pcm[cursor++];
      pending_ = true;
    }
    size_t position = 0;
    while (position < count) {
      audio::ResampleProgress progress;
      const auto status =
          resampler_.Process(std::span(decoded).first(count).subspan(position),
                             converted, &progress);
      if (!status || (!progress.consumed && !progress.produced))
        return Fail("realtime_audio_conversion_failed",
                    "Audio conversion made no valid progress");
      position += progress.consumed;
      reply = Write(std::span(converted).first(progress.produced), sink);
      if (!reply.ok()) return reply;
    }
  }
  return Check();
}

ObjectReply RealtimeSpeechEncoder::Finish(const Sink& sink,
                                          RealtimeSpeechReceipt* receipt) {
  auto reply = Check();
  if (!reply.ok()) return reply;
  if (!sink || !receipt || pending_ || !input_bytes_)
    return Fail("realtime_audio_incomplete", "PCM ended without full samples");
  std::array<float, 1024> converted;
  do {
    size_t produced = 0;
    const auto status = resampler_.Finish(converted, &produced);
    if (!status)
      return Fail("realtime_audio_conversion_failed", "Audio tail failed");
    reply = Write(std::span(converted).first(produced), sink);
    if (!reply.ok()) return reply;
  } while (!resampler_.Drained());
  const size_t width =
      format_.encoding == RealtimeAudioEncoding::kPcm16 ? 2 : 1;
  const uint64_t samples = input_bytes_ / 2;
  if (!output_bytes_ || resampler_.InputSamples() != samples ||
      resampler_.OutputSamples() != samples * format_.rate / 24000 ||
      output_bytes_ != resampler_.OutputSamples() * width)
    return Fail("realtime_audio_incomplete", "Audio extent did not complete");
  finished_ = true;
  *receipt = {samples, resampler_.OutputSamples(), output_bytes_};
  return {};
}
}  // namespace q4t::server
