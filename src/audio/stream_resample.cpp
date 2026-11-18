#include "q4t/audio/stream_resample.h"

#include <algorithm>
#include <cmath>
#include <numbers>

namespace q4t::audio {
namespace {
constexpr uint32_t kOutputRate = 16000;
constexpr int kRadius = 48;
bool Overlap(std::span<const float> input, std::span<float> output) {
  if (input.empty() || output.empty()) return false;
  const auto a = reinterpret_cast<uintptr_t>(input.data());
  const auto b = reinterpret_cast<uintptr_t>(output.data());
  return a <= b ? b - a < input.size_bytes() : a - b < output.size_bytes();
}
}  // namespace

AsrStreamResampler::AsrStreamResampler(uint32_t input_rate,
                                       uint64_t maximum_input_samples)
    : rate_(input_rate), maximum_(maximum_input_samples) {
  valid_ =
      (rate_ == 8000 || rate_ == 16000 || rate_ == 24000 || rate_ == 48000) &&
      maximum_ && maximum_ <= uint64_t(rate_) * 3600;
  if (!valid_ || rate_ == kOutputRate) return;
  const double cutoff = .45 * std::min(rate_, kOutputRate) / rate_;
  for (size_t phase = 0; phase < weights_.size(); ++phase) {
    double total = 0;
    for (int tap = -kRadius; tap <= kRadius; ++tap) {
      const double x = tap - .5 * phase;
      if (std::abs(x) >= kRadius) continue;
      const double angle = 2 * std::numbers::pi * cutoff * x;
      const double sinc = x == 0 ? 1. : std::sin(angle) / angle;
      const double window = .42 +
                            .5 * std::cos(std::numbers::pi * x / kRadius) +
                            .08 * std::cos(2 * std::numbers::pi * x / kRadius);
      const double value = 2 * cutoff * sinc * window;
      weights_[phase][tap + kRadius] = value;
      total += value;
    }
    for (double& value : weights_[phase]) value /= total;
  }
}

uint64_t AsrStreamResampler::FinalCount() const {
  return received_ * kOutputRate / rate_;
}
bool AsrStreamResampler::Ready() const {
  if (!valid_ || cancelled_ || emitted_ >= FinalCount()) return false;
  if (sealed_) return true;
  const auto center = emitted_ * rate_ / kOutputRate;
  return center + (rate_ == kOutputRate ? 0 : kRadius) < received_;
}
float AsrStreamResampler::Sample() const {
  const uint64_t position = emitted_ * rate_;
  const auto center = static_cast<int64_t>(position / kOutputRate);
  if (rate_ == kOutputRate) return history_[center % history_.size()];
  const auto phase = (position % kOutputRate) / 8000;
  double sum = 0;
  for (int tap = -kRadius; tap <= kRadius; ++tap) {
    const int64_t source = center + tap;
    if (source >= 0 && uint64_t(source) < received_)
      sum += double(history_[uint64_t(source) % history_.size()]) *
             weights_[phase][tap + kRadius];
  }
  return static_cast<float>(sum);
}

Status AsrStreamResampler::Process(std::span<const float> input,
                                   std::span<float> output,
                                   ResampleProgress* progress) {
  if (progress) *progress = {};
  if (!progress || !valid_ || sealed_ || cancelled_ ||
      input.size() > (1ULL << 20) || input.size() > maximum_ - received_ ||
      Overlap(input, output) ||
      !std::all_of(input.begin(), input.end(), [](float value) {
        return std::isfinite(value) && std::abs(value) <= 1;
      }))
    return Status::Fail("stream resampling input/state invalid");
  while (progress->produced < output.size()) {
    if (Ready()) {
      output[progress->produced++] = Sample();
      ++emitted_;
    } else if (progress->consumed < input.size()) {
      history_[received_ % history_.size()] = input[progress->consumed++];
      ++received_;
    } else
      break;
  }
  return {};
}

Status AsrStreamResampler::Finish(std::span<float> output, size_t* produced) {
  if (produced) *produced = 0;
  if (!produced || !valid_ || cancelled_)
    return Status::Fail("stream resampling finish state invalid");
  sealed_ = true;
  while (*produced < output.size() && Ready()) {
    output[(*produced)++] = Sample();
    ++emitted_;
  }
  return {};
}
bool AsrStreamResampler::Drained() const {
  return valid_ && sealed_ && !cancelled_ && emitted_ == FinalCount();
}
void AsrStreamResampler::Cancel() {
  cancelled_ = true;
  sealed_ = true;
  history_.fill(0);
}
}  // namespace q4t::audio
