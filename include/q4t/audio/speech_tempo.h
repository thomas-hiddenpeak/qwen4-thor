// Streaming native speech time scaling; no model or external codec dependency.
#pragma once

#include <array>
#include <span>
#include <vector>

#include "q4t/status.h"

namespace q4t::audio {
enum class SpeechTempoStep { kNeedInput, kOutput, kDone };

// 24 kHz mono overlap-add with a bounded waveform-similarity search. Each
// Advance emits at most 10 ms, so the owner can check deadline/cancellation.
// Input and output are separately bounded. Feed sizes do not change the math.
class SpeechTempo {
 public:
  static constexpr size_t kHop = 240;
  static constexpr size_t kWindow = 2 * kHop;
  static constexpr size_t kSearch = 120;
  static constexpr size_t kMaxAppend = 24000;
  static constexpr size_t kBufferSamples = 65536;
  Status Init(double speed, size_t max_input_samples,
              size_t max_output_samples);
  // Append only when Advance requested input; empty final closes a stream.
  // Invalid input leaves the existing stream unchanged.
  Status Append(std::span<const float> samples, bool final);
  Status Advance(SpeechTempoStep* step, std::span<const float>* output);
  size_t input_samples() const { return received_; }
  size_t output_samples() const { return produced_; }
  size_t buffered_samples() const { return input_.size(); }

 private:
  float Sample(size_t position) const;
  size_t Nominal() const;
  size_t OutputCount(size_t input) const;
  void Discard();
  double speed_ = 1;
  size_t max_input_ = 0, max_output_ = 0;
  size_t received_ = 0, produced_ = 0, begin_ = 0, previous_ = 0;
  bool initialized_ = false, need_input_ = true, final_ = false, done_ = false;
  std::vector<float> input_;
  std::array<float, kHop> overlap_{}, output_{}, fade_{};
};
}  // namespace q4t::audio
