// Stateful bounded 16 kHz noise suppression. No model/runtime dependency.
#pragma once

#include <array>
#include <span>

#include "q4t/audio/stream_resample.h"

namespace q4t::audio {
enum class NoiseReduction { kOff, kNearField, kFarField };
// Native profile: 512-point sqrt-Hann WOLA, hop256, rolling spectral minimum
// noise estimate and decision-directed Wiener gain. Near-field gain floor .18;
// far-field .35 retains more low-level speech. This does not remove competing
// speakers or promise dereverberation. Full runtime qualification is separate.
class NoiseStream {
 public:
  explicit NoiseStream(NoiseReduction mode, uint64_t maximum_samples);
  bool Valid() const { return valid_; }
  // At most 1024 input samples per call; no allocation or model work. Output
  // pressure leaves unconsumed input with the caller. Validate input first.
  Status Process(std::span<const float> input, std::span<float> output,
                 ResampleProgress* progress);
  // Normal finish drains exactly InputSamples() samples, excluding FFT padding.
  // Enabled processing has 256 samples lookahead; timestamps are not shifted.
  Status Finish(std::span<float> output, size_t* produced);
  void Cancel();
  bool Drained() const;
  uint64_t InputSamples() const { return received_; }
  uint64_t OutputSamples() const { return emitted_; }

 private:
  bool Transform();
  size_t Drain(std::span<float> output);
  const NoiseReduction mode_;
  const uint64_t maximum_;
  std::array<float, 256> previous_{}, input_{}, pending_{};
  std::array<double, 256> overlap_{};
  std::array<double, 257> smooth_{}, noise_{}, old_power_{}, old_gain_{};
  std::array<std::array<double, 257>, 8> minimum_{};
  size_t filled_ = 0, pending_begin_ = 0, pending_size_ = 0;
  uint64_t received_ = 0, emitted_ = 0, frames_ = 0;
  bool valid_ = false, sealed_ = false, cancelled_ = false, failed_ = false;
};
}  // namespace q4t::audio
