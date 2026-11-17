#include "q4t/audio/features.h"

#include <algorithm>
#include <array>
#include <cmath>
#include <cstdint>
#include <limits>
#include <new>
#include <numbers>
#include <utility>

#include "q4t/audio/asr_config.h"

namespace q4t::audio {
namespace {
using C = AsrConfig;
constexpr size_t kFrequencies = C::kFft / 2 + 1;

// The reference is WhisperFeatureExtractor/transformers.audio_utils, Apache
// 2.0 (HuggingFace, librosa and torchaudio authors); see ASR source attribution.
// These standard DFT/Slaney equations are implemented independently here.
struct Tables {
  std::array<double, C::kFft> window;
  std::array<double, kFrequencies * C::kFft> real;
  std::array<double, kFrequencies * C::kFft> imaginary;
  std::array<double, C::kMelBins * kFrequencies> filters;

  Tables() {
    constexpr double pi = std::numbers::pi_v<double>;
    for (int n = 0; n < C::kFft; ++n)
      window[n] = 0.5 - 0.5 * std::cos(2 * pi * n / C::kFft);
    for (size_t k = 0; k < kFrequencies; ++k)
      for (int n = 0; n < C::kFft; ++n) {
        const double phase = 2 * pi * k * n / C::kFft;
        real[k * C::kFft + n] = std::cos(phase);
        imaginary[k * C::kFft + n] = -std::sin(phase);
      }
    const double log_step = std::log(6.4) / 27;
    const double max_mel = 15 + std::log(8.0) / log_step;
    std::array<double, C::kMelBins + 2> frequencies;
    for (size_t i = 0; i < frequencies.size(); ++i) {
      const double mel = max_mel * i / (C::kMelBins + 1);
      frequencies[i] = mel < 15 ? mel * (200.0 / 3)
                                : 1000 * std::exp(log_step * (mel - 15));
    }
    for (int m = 0; m < C::kMelBins; ++m) {
      const double left = frequencies[m], center = frequencies[m + 1];
      const double right = frequencies[m + 2];
      for (size_t k = 0; k < kFrequencies; ++k) {
        const double hz = static_cast<double>(k) * C::kSampleRate / C::kFft;
        const double triangle = std::max(0.0, std::min(
            (hz - left) / (center - left), (right - hz) / (right - center)));
        filters[m * kFrequencies + k] = triangle * (2 / (right - left));
      }
    }
  }
};

size_t Reflected(int64_t index, size_t length) {
  const auto period = static_cast<int64_t>(2 * (length - 1));
  index %= period;
  if (index < 0) index += period;
  if (index >= static_cast<int64_t>(length)) index = period - index;
  return static_cast<size_t>(index);
}

Status Cancelled() {
  return Status::Fail("ASR feature computation cancelled");
}
}  // namespace

Status ComputeAsrFeatures(std::span<const float> samples, int sample_rate,
                          const AsrFeatureLimits& limits, AsrFeatures* out,
                          const std::function<bool()>& cancelled) {
  if (!out || sample_rate != C::kSampleRate)
    return Status::Fail("ASR features require an output and 16 kHz mono PCM");
  if (cancelled && cancelled()) return Cancelled();
  if (samples.size() < C::kHop || samples.size() > limits.max_samples ||
      samples.size() > static_cast<size_t>(INT64_MAX / 2))
    return Status::Fail("ASR sample count is outside the feature budget");
  const size_t frames = samples.size() / C::kHop;
  if (frames > limits.max_frames ||
      frames > std::numeric_limits<size_t>::max() / C::kMelBins / sizeof(float))
    return Status::Fail("ASR frame count exceeds the feature budget");
  for (size_t i = 0; i < samples.size(); ++i) {
    if (!std::isfinite(samples[i]))
      return Status::Fail("ASR PCM contains a non-finite sample");
    if ((i & 4095) == 0 && cancelled && cancelled()) return Cancelled();
  }
  static const Tables tables;
  AsrFeatures result;
  result.frames = frames;
  try {
    result.mel.resize(frames * C::kMelBins);
  } catch (const std::bad_alloc&) {
    return Status::Fail("ASR feature allocation failed");
  }
  std::array<double, C::kFft> windowed;
  std::array<double, kFrequencies> powers;
  float maximum = -std::numeric_limits<float>::infinity();
  for (size_t t = 0; t < frames; ++t) {
    if (cancelled && cancelled()) return Cancelled();
    const int64_t first = static_cast<int64_t>(t * C::kHop) - C::kFft / 2;
    for (int n = 0; n < C::kFft; ++n)
      windowed[n] = samples[Reflected(first + n, samples.size())] *
                    tables.window[n];
    for (size_t k = 0; k < kFrequencies; ++k) {
      double real = 0, imaginary = 0;
      for (int n = 0; n < C::kFft; ++n) {
        real += windowed[n] * tables.real[k * C::kFft + n];
        imaginary += windowed[n] * tables.imaginary[k * C::kFft + n];
      }
      // The NumPy reference retains complex64 FFT coefficients before
      // computing the power in float64; keep this explicit rounding point.
      const double rounded_real = static_cast<float>(real);
      const double rounded_imaginary = static_cast<float>(imaginary);
      powers[k] = rounded_real * rounded_real +
                  rounded_imaginary * rounded_imaginary;
    }
    for (int m = 0; m < C::kMelBins; ++m) {
      double energy = 0;
      for (size_t k = 0; k < kFrequencies; ++k)
        energy += tables.filters[m * kFrequencies + k] * powers[k];
      const auto value = static_cast<float>(std::log10(std::max(energy, 1e-10)));
      if (!std::isfinite(value))
        return Status::Fail("ASR spectral energy is non-finite");
      result.mel[m * frames + t] = value;
      maximum = std::max(maximum, value);
    }
  }
  const float floor = maximum - 8.0f;
  for (size_t i = 0; i < result.mel.size(); ++i) {
    if ((i & 4095) == 0 && cancelled && cancelled()) return Cancelled();
    result.mel[i] = (std::max(result.mel[i], floor) + 4.0f) / 4.0f;
  }
  if (cancelled && cancelled()) return Cancelled();
  *out = std::move(result);
  return {};
}

}  // namespace q4t::audio
