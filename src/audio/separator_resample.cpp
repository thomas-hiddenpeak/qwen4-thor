#include "q4t/audio/separator_resample.h"

#include <algorithm>
#include <array>
#include <cmath>
#include <numbers>

namespace q4t::audio {
namespace {
const std::array<double, 65>& Kernel() {
  static const auto kernel = [] {
    std::array<double, 65> values{};
    double total = 0;
    for (int k = -32; k <= 32; ++k) {
      const double phase = std::numbers::pi * .45 * k;
      const double sinc = k ? std::sin(phase) / phase : 1.;
      const double hann = .5 + .5 * std::cos(std::numbers::pi * k / 32.);
      values[k + 32] = .45 * sinc * hann;
      total += values[k + 32];
    }
    for (auto& value : values) value /= total;
    return values;
  }();
  return kernel;
}
Status Resample(std::span<const float> input, size_t count, bool downsample,
                std::vector<float>* out,
                const std::function<bool()>& cancelled) {
  if (cancelled && cancelled()) return Status::Fail("resampling cancelled");
  if (!std::all_of(input.begin(), input.end(),
                   [](float value) { return std::isfinite(value); }))
    return Status::Fail("resampling input is nonfinite");
  std::vector<float> output(count);
  const auto& kernel = Kernel();
  for (size_t i = 0; i < count; ++i) {
    if (i % 256 == 0 && cancelled && cancelled())
      return Status::Fail("resampling cancelled");
    const int64_t at = downsample ? int64_t(i * 2) : int64_t(i);
    double sum = 0;
    for (int tap = -32; tap <= 32; ++tap) {
      int64_t source = at - tap;
      if (!downsample) {
        if (source % 2 != 0) continue;
        source /= 2;
      }
      if (source >= 0 && uint64_t(source) < input.size())
        sum += double(input[source]) * kernel[tap + 32];
    }
    output[i] = float(sum * (downsample ? 1. : 2.));
    if (!std::isfinite(output[i]))
      return Status::Fail("resampling output is nonfinite");
  }
  if (cancelled && cancelled()) return Status::Fail("resampling cancelled");
  *out = std::move(output);
  return {};
}
}  // namespace

Status DownsampleSeparation(std::span<const float> input,
                            size_t max_input_samples,
                            std::vector<float>* output,
                            const std::function<bool()>& cancelled) {
  if (!output || input.empty() || input.size() > max_input_samples ||
      input.size() > kSeparationMaxInputSamples)
    return Status::Fail("separation resampling input budget invalid");
  return Resample(input, (input.size() + 1) / 2, true, output, cancelled);
}
Status RestoreSeparation(std::span<const float> input, size_t original_samples,
                         size_t max_output_samples, std::vector<float>* output,
                         const std::function<bool()>& cancelled) {
  if (!output || !original_samples || original_samples > max_output_samples ||
      original_samples > kSeparationMaxInputSamples ||
      input.size() != (original_samples + 1) / 2)
    return Status::Fail("separation restoration shape or budget invalid");
  return Resample(input, original_samples, false, output, cancelled);
}
Status NormalizeSeparation(std::span<const float> input,
                           size_t max_output_samples,
                           std::vector<float>* output,
                           const std::function<bool()>& cancelled) {
  if (!output || input.empty() || input.size() > max_output_samples ||
      input.size() > kSeparationMaxInputSamples)
    return Status::Fail("separation normalization shape or budget invalid");
  double peak = 0;
  for (size_t i = 0; i < input.size(); ++i) {
    if (i % 256 == 0 && cancelled && cancelled())
      return Status::Fail("separation normalization cancelled");
    if (!std::isfinite(input[i]))
      return Status::Fail("separation normalization input nonfinite");
    peak = std::max(peak, std::abs(double(input[i])));
  }
  std::vector<float> result(input.size(), 0);
  for (size_t i = 0; i < input.size(); ++i) {
    if (i % 256 == 0 && cancelled && cancelled())
      return Status::Fail("separation normalization cancelled");
    if (peak > 0) result[i] = float(double(input[i]) / peak);
  }
  if (cancelled && cancelled())
    return Status::Fail("separation normalization cancelled");
  *output = std::move(result);
  return {};
}
}  // namespace q4t::audio
