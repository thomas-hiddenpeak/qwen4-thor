// Continuous wire PCM/G.711 -> one shared denoised native ASR/VAD waveform.
#pragma once

#include "q4t/audio/noise_reduction.h"
#include "q4t/server/realtime_audio_queue.h"

#include <functional>

namespace q4t::server {
class RealtimeNoiseInput {
 public:
  using Sink = std::function<Status(std::span<const float>)>;
  RealtimeNoiseInput(RealtimeAudioFormat format, audio::NoiseReduction mode,
                     uint64_t begin_ticks,
                     std::function<bool()> cancelled = {});
  bool Valid() const { return valid_; }
  // Serial session-owner calls. CPU work checks cancellation between fixed
  // chunks; caller keeps the socket reader independent and bounds its inbox.
  // Finite resampler overshoot is saturated to [-1,1] before noise reduction;
  // the original no-noise decoder/resampler contract is unchanged.
  Status Append(std::span<const uint8_t> bytes, const Sink& sink);
  Status Finish(const Sink& sink);
  bool PartialSample() const { return partial_; }
  uint64_t AcceptedTicks() const;
  uint64_t FinalSamples() const {
    return valid_ ? received_ * 16000 / format_.rate : 0;
  }
  uint64_t DeliveredSamples() const { return delivered_; }

 private:
  Status Feed(std::span<const float> input, const Sink& sink);
  Status Reduce(std::span<const float> input, const Sink& sink);
  bool Interrupted() const;
  const RealtimeAudioFormat format_;
  const uint64_t origin_;
  const std::function<bool()> cancelled_;
  audio::AsrStreamResampler resampler_;
  audio::NoiseStream noise_;
  uint64_t received_ = 0, delivered_ = 0;
  uint8_t low_byte_ = 0;
  bool valid_ = false, partial_ = false, sealed_ = false, failed_ = false;
};
}  // namespace q4t::server
