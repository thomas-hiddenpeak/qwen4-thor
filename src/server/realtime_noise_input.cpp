#include "q4t/server/realtime_noise_input.h"

#include <algorithm>
#include <cmath>

#include "q4t/server/realtime_pcm.h"

namespace q4t::server {
namespace {
Status Invalid() { return Status::Fail("realtime noise input state invalid"); }
}  // namespace
RealtimeNoiseInput::RealtimeNoiseInput(RealtimeAudioFormat format,
                                       audio::NoiseReduction mode,
                                       uint64_t begin_ticks,
                                       std::function<bool()> cancelled)
    : format_(format),
      origin_(begin_ticks),
      cancelled_(std::move(cancelled)),
      resampler_(format.rate, uint64_t(format.rate) * 3600),
      noise_(mode, 3600ULL * 16000) {
  valid_ = format.encoding != RealtimeAudioEncoding::kFloat32 &&
           RealtimeAudioQueue(1, format).Valid() &&
           origin_ <= kRealtimeSessionTicks && resampler_.Valid() &&
           noise_.Valid();
}
uint64_t RealtimeNoiseInput::AcceptedTicks() const {
  return valid_ ? origin_ + received_ * (kRealtimeTicksPerSecond / format_.rate)
                : origin_;
}
bool RealtimeNoiseInput::Interrupted() const {
  return cancelled_ && cancelled_();
}
Status RealtimeNoiseInput::Reduce(std::span<const float> input,
                                  const Sink& sink) {
  // The unchanged sinc resampler can overshoot normalized PCM at sharp edges.
  // Saturate once at the optional processor boundary; both VAD and ASR then
  // consume the same processed waveform. Never turn NaN/Inf into valid audio.
  std::array<float, 512> normalized;
  if (input.size() > normalized.size()) return Invalid();
  for (size_t i = 0; i < input.size(); ++i) {
    if (!std::isfinite(input[i])) return Invalid();
    normalized[i] = std::clamp(input[i], -1.0f, 1.0f);
  }
  input = std::span(normalized).first(input.size());
  std::array<float, 512> output;
  size_t at = 0;
  for (;;) {
    if (Interrupted()) return Invalid();
    audio::ResampleProgress progress;
    auto status = noise_.Process(input.subspan(at), output, &progress);
    if (!status) return status;
    at += progress.consumed;
    if (progress.produced) {
      status = sink(std::span(output).first(progress.produced));
      if (!status) return status;
      delivered_ += progress.produced;
    }
    if (at == input.size() && progress.produced == 0) return {};
    if (progress.consumed == 0 && progress.produced == 0) return Invalid();
  }
}
Status RealtimeNoiseInput::Feed(std::span<const float> input,
                                const Sink& sink) {
  std::array<float, 512> output;
  size_t at = 0;
  for (;;) {
    if (Interrupted()) return Invalid();
    audio::ResampleProgress progress;
    auto status = resampler_.Process(input.subspan(at), output, &progress);
    if (!status) return status;
    at += progress.consumed;
    status = Reduce(std::span(output).first(progress.produced), sink);
    if (!status) return status;
    if (at == input.size() && progress.produced == 0) return {};
    if (progress.consumed == 0 && progress.produced == 0) return Invalid();
  }
}
Status RealtimeNoiseInput::Append(std::span<const uint8_t> bytes,
                                  const Sink& sink) {
  if (!valid_ || sealed_ || failed_ || !sink || Interrupted() ||
      bytes.size() > kRealtimeAudioBytes)
    return Invalid();
  const bool pcm = format_.encoding == RealtimeAudioEncoding::kPcm16;
  const uint64_t samples = (bytes.size() + size_t(partial_)) / (pcm ? 2 : 1);
  if (samples > (kRealtimeSessionTicks - AcceptedTicks()) /
                    (kRealtimeTicksPerSecond / format_.rate))
    return Invalid();
  size_t at = 0;
  std::array<float, 1024> decoded;
  while (at < bytes.size()) {
    if (Interrupted()) {
      failed_ = true;
      return Invalid();
    }
    size_t count = 0;
    while (at < bytes.size() && count < decoded.size()) {
      if (!pcm) {
        decoded[count++] = DecodeRealtimeSample(format_.encoding, bytes[at++]);
      } else {
        if (!partial_) {
          low_byte_ = bytes[at++];
          partial_ = true;
        }
        if (at == bytes.size()) break;
        decoded[count++] =
            DecodeRealtimeSample(format_.encoding, low_byte_, bytes[at++]);
        partial_ = false;
      }
    }
    received_ += count;
    const auto status = Feed(std::span(decoded).first(count), sink);
    if (!status) {
      failed_ = true;
      return status;
    }
  }
  return {};
}
Status RealtimeNoiseInput::Finish(const Sink& sink) {
  if (!valid_ || failed_ || partial_ || !sink || Interrupted())
    return Invalid();
  sealed_ = true;
  std::array<float, 512> output;
  while (!resampler_.Drained()) {
    if (Interrupted()) {
      failed_ = true;
      return Invalid();
    }
    size_t count = 0;
    auto status = resampler_.Finish(output, &count);
    if (status) status = Reduce(std::span(output).first(count), sink);
    if (!status) {
      failed_ = true;
      return status;
    }
  }
  while (!noise_.Drained()) {
    if (Interrupted()) {
      failed_ = true;
      return Invalid();
    }
    size_t count = 0;
    auto status = noise_.Finish(output, &count);
    if (status && count) status = sink(std::span(output).first(count));
    if (!status) {
      failed_ = true;
      return status;
    }
    delivered_ += count;
  }
  if (resampler_.InputSamples() != received_ ||
      noise_.InputSamples() != FinalSamples() || delivered_ != FinalSamples()) {
    failed_ = true;
    return Invalid();
  }
  return {};
}
}  // namespace q4t::server
