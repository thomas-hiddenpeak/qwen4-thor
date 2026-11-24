#include "q4t/audio/noise_reduction.h"

#include <algorithm>
#include <cmath>
#include <limits>
#include <numbers>

#include "noise_fft.h"

namespace q4t::audio {
namespace {
bool Overlap(std::span<const float> input, std::span<float> output) {
  if (input.empty() || output.empty()) return false;
  const auto a = reinterpret_cast<uintptr_t>(input.data());
  const auto b = reinterpret_cast<uintptr_t>(output.data());
  return a <= b ? b - a < input.size_bytes() : a - b < output.size_bytes();
}
struct Window {
  std::array<double, 512> values;
  Window() {
    for (size_t n = 0; n < values.size(); ++n)
      values[n] = std::sqrt(
          .5 - .5 * std::cos(2 * std::numbers::pi * double(n) / values.size()));
  }
};
Status Invalid() { return Status::Fail("noise stream input or state invalid"); }
}  // namespace

NoiseStream::NoiseStream(NoiseReduction mode, uint64_t maximum_samples,
                         double strength)
    : mode_(mode), maximum_(maximum_samples), strength_(strength) {
  valid_ =
      (mode == NoiseReduction::kOff || mode == NoiseReduction::kNearField ||
       mode == NoiseReduction::kFarField) &&
      strength >= 0 && strength <= 1 &&
      maximum_samples && maximum_samples <= 3600ULL * 16000;
  for (auto& bucket : minimum_)
    bucket.fill(std::numeric_limits<double>::infinity());
  old_gain_.fill(1);
}

bool NoiseStream::Transform() {
  static const Window window;
  detail::NoiseSpectrum spectrum;
  for (size_t i = 0; i < 256; ++i) {
    spectrum[i] = previous_[i] * window.values[i];
    spectrum[256 + i] = input_[i] * window.values[256 + i];
  }
  detail::NoiseFft(&spectrum, false);
  const size_t bucket = (frames_ / 8) % minimum_.size();
  if (frames_ % 8 == 0)
    minimum_[bucket].fill(std::numeric_limits<double>::infinity());
  const double floor = mode_ == NoiseReduction::kNearField ? .18 : .35;
  for (size_t k = 0; k <= 256; ++k) {
    const double power = std::norm(spectrum[k]);
    if (!std::isfinite(power)) return false;
    smooth_[k] = frames_ ? .8 * smooth_[k] + .2 * power : power;
    minimum_[bucket][k] = std::min(minimum_[bucket][k], smooth_[k]);
    double minimum = minimum_[bucket][k];
    for (const auto& history : minimum_)
      minimum = std::min(minimum, history[k]);
    const double estimate = std::min(power, 1.5 * minimum);
    noise_[k] = frames_ ? .95 * noise_[k] + .05 * estimate : .05 * power;
    const double denominator = std::max(noise_[k], 1e-20);
    const double posterior = std::max(0., power / denominator - 1);
    const double prior = frames_ ? .98 * old_gain_[k] * old_gain_[k] *
                                           old_power_[k] / denominator +
                                       .02 * posterior
                                 : posterior;
    const double gain = std::max(floor, prior / (1 + prior));
    if (!std::isfinite(gain)) return false;
    old_gain_[k] = gain;
    old_power_[k] = power;
    const double applied = std::lerp(1., gain, strength_);
    spectrum[k] *= applied;
    if (k && k != 256) spectrum[512 - k] *= applied;
  }
  detail::NoiseFft(&spectrum, true);
  for (size_t i = 0; i < 256; ++i) {
    const double value = spectrum[i].real() * window.values[i] + overlap_[i];
    const double tail = spectrum[256 + i].real() * window.values[256 + i];
    if (!std::isfinite(value) || !std::isfinite(tail)) return false;
    pending_[i] = float(std::clamp(value, -1., 1.));
    overlap_[i] = tail;
  }
  previous_ = input_;
  input_.fill(0);
  filled_ = pending_begin_ = 0;
  // The first synthesized hop belongs to virtual negative-time padding.
  pending_size_ = frames_++ ? 256 : 0;
  return true;
}
size_t NoiseStream::Drain(std::span<float> output) {
  const auto count = std::min<uint64_t>(
      std::min(output.size(), pending_size_ - pending_begin_),
      received_ - emitted_);
  std::copy_n(pending_.begin() + pending_begin_, count, output.begin());
  pending_begin_ += count;
  emitted_ += count;
  return count;
}
Status NoiseStream::Process(std::span<const float> input,
                            std::span<float> output,
                            ResampleProgress* progress) {
  if (progress) *progress = {};
  if (!progress || !valid_ || sealed_ || cancelled_ || failed_ ||
      input.size() > 1024 || input.size() > maximum_ - received_ ||
      Overlap(input, output) ||
      !std::all_of(input.begin(), input.end(), [](float v) {
        return std::isfinite(v) && std::abs(v) <= 1;
      }))
    return Invalid();
  if (mode_ == NoiseReduction::kOff) {
    const auto count = std::min(input.size(), output.size());
    std::copy_n(input.begin(), count, output.begin());
    progress->consumed = progress->produced = count;
    received_ += count;
    emitted_ += count;
    return {};
  }
  while (progress->produced < output.size()) {
    progress->produced += Drain(output.subspan(progress->produced));
    if (pending_begin_ != pending_size_ || progress->consumed == input.size())
      break;
    const auto count =
        std::min(size_t{256} - filled_, input.size() - progress->consumed);
    std::copy_n(input.begin() + progress->consumed, count,
                input_.begin() + filled_);
    filled_ += count;
    received_ += count;
    progress->consumed += count;
    if (filled_ == 256 && !Transform()) {
      failed_ = true;
      return Invalid();
    }
  }
  return {};
}
Status NoiseStream::Finish(std::span<float> output, size_t* produced) {
  if (produced) *produced = 0;
  if (!produced || !valid_ || cancelled_ || failed_) return Invalid();
  sealed_ = true;
  while (*produced < output.size() && emitted_ < received_) {
    *produced += Drain(output.subspan(*produced));
    if (*produced == output.size() || emitted_ == received_) break;
    if (!Transform()) {
      failed_ = true;
      return Invalid();
    }
  }
  return {};
}
void NoiseStream::Cancel() {
  cancelled_ = sealed_ = true;
  input_.fill(0);
  previous_.fill(0);
  pending_.fill(0);
  overlap_.fill(0);
}
bool NoiseStream::Drained() const {
  return valid_ && sealed_ && !cancelled_ && !failed_ && emitted_ == received_;
}
}  // namespace q4t::audio
