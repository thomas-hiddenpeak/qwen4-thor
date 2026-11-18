// Streaming native speech time scaling; no model or external codec dependency.
#pragma once

#include <array>
#include <span>
#include <vector>

#include "q4t/status.h"

namespace q4t::audio {
enum class SpeechTempoStep { kNeedInput, kOutput, kDone };

namespace detail {
// One overlap-add stage, restricted to speeds at most two.
class SpeechTempoStage {
 public:
  static constexpr size_t kHop = 240;
  static constexpr size_t kWindow = 2 * kHop;
  static constexpr size_t kSearch = 120;
  static constexpr size_t kMaxAppend = 24000;
  static constexpr size_t kBufferSamples = 32768;
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
}  // namespace detail

// 24 kHz mono waveform-similarity overlap-add. Speeds above two use two
// stages so analysis windows cannot skip an entire input window. Each
// Advance runs at most eight bounded grains and emits at most 10 ms.
// Input/output limits and the sum of both input buffers are bounded.
class SpeechTempo {
 public:
  static constexpr size_t kHop = detail::SpeechTempoStage::kHop;
  static constexpr size_t kMaxAppend = detail::SpeechTempoStage::kMaxAppend;
  static constexpr size_t kBufferSamples =
      2 * detail::SpeechTempoStage::kBufferSamples;
  Status Init(double speed, size_t max_input_samples,
              size_t max_output_samples);
  // Append only when Advance requested input. Invalid input is atomic.
  Status Append(std::span<const float> samples, bool final);
  Status Advance(SpeechTempoStep* step, std::span<const float>* output);
  size_t input_samples() const { return first_.input_samples(); }
  size_t output_samples() const { return produced_; }
  size_t buffered_samples() const {
    return first_.buffered_samples() + second_.buffered_samples();
  }

 private:
  detail::SpeechTempoStage first_, second_;
  double speed_ = 1;
  size_t max_output_ = 0, produced_ = 0;
  bool initialized_ = false, final_ = false, done_ = false;
  bool second_final_ = false;
};
}  // namespace q4t::audio
