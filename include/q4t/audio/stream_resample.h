// Constant-memory normalized PCM conversion for continuous ASR input.
#pragma once

#include <array>
#include <cstddef>
#include <cstdint>
#include <span>

#include "q4t/status.h"

namespace q4t::audio {
struct ResampleProgress {
  size_t consumed = 0;
  size_t produced = 0;
};

class AsrStreamResampler {
 public:
  // 8/16/24/48 kHz mono normalized PCM -> 16 kHz by default. Explicit
  // 8/24 kHz output supports returning actual processed audio to the client.
  // Equal rates are bit-exact bypass. Existing default ASR math is unchanged.
  // Otherwise: centered 97-tap Blackman sinc, cutoff .45*min(input,output),
  // unity DC per fractional phase, FP64 sum -> FP32, zero extension at ends.
  // At most 48 input samples of lookahead, not an extra timestamp offset.
  // Output count is floor(total_input*output_rate/rate), not per-chunk rounding.
  explicit AsrStreamResampler(uint32_t input_rate,
                              uint64_t maximum_input_samples,
                              uint32_t output_rate = 16000,
                              bool normalized_input = true);
  // Exporting already processed PCM can explicitly accept finite sinc
  // overshoot. This never changes the default normalized ASR input contract.
  bool Valid() const { return valid_; }
  // Fixed 128-sample ring. Caller owns bounded output; a full output stops
  // consumption, preserving unwritten input for retry. No hidden queue/heap.
  // Finite input in [-1,1], <=1M samples/call, non-overlapping spans. Validate
  // the whole supplied input before mutation; errors leave output unchanged.
  Status Process(std::span<const float> input, std::span<float> output,
                 ResampleProgress* progress);
  // Seal input once, then repeatedly drain the exact tail into bounded output.
  // No padded extra samples or fake completion. Process after Finish fails.
  Status Finish(std::span<float> output, size_t* produced);
  void Cancel();
  bool Drained() const;
  bool Cancelled() const { return cancelled_; }
  uint64_t InputSamples() const { return received_; }
  uint64_t OutputSamples() const { return emitted_; }

 private:
  bool Ready() const;
  float Sample() const;
  uint64_t FinalCount() const;
  const uint32_t rate_, output_rate_;
  const uint64_t maximum_;
  const bool normalized_input_;
  bool valid_ = false, sealed_ = false, cancelled_ = false;
  uint64_t received_ = 0, emitted_ = 0;
  std::array<float, 128> history_{};
  std::array<std::array<double, 97>, 3> weights_{};
};
}  // namespace q4t::audio
