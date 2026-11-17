#include "q4t/audio/speaker_features.h"

#include <algorithm>
#include <array>
#include <cmath>
#include <cstdint>
#include <limits>
#include <new>
#include <numbers>

#include "q4t/audio/speaker_config.h"

namespace q4t::audio {
namespace {
using C = SpeakerConfig;
constexpr size_t kFrequencies = C::kFft / 2 + 1;
struct Tables {
  std::array<float, C::kFft> window;
  std::array<double, kFrequencies * C::kFft> real, imaginary;
  std::array<float, C::kMelBins * kFrequencies> filters;
  Tables() {
    const double pi = std::numbers::pi_v<double>;
    for (int n = 0; n < C::kFft; ++n)
      window[n] = 0.54 - 0.46 * std::cos(2 * pi * n / C::kFft);
    for (size_t k = 0; k < kFrequencies; ++k)
      for (int n = 0; n < C::kFft; ++n) {
        const double phase = 2 * pi * k * n / C::kFft;
        real[k * C::kFft + n] = std::cos(phase);
        imaginary[k * C::kFft + n] = -std::sin(phase);
      }
    // SpeechBrain uses the left spacing for both triangle halves. This is
    // neither Slaney normalization nor a triangle joining adjacent centers.
    const float maximum = 2595 * std::log10(1 + 8000.0 / 700);
    std::array<float, C::kMelBins + 2> frequencies;
    for (size_t i = 0; i < frequencies.size(); ++i) {
      const float mel = maximum * i / (C::kMelBins + 1);
      frequencies[i] = 700.f * (std::pow(10.f, mel / 2595.f) - 1.f);
    }
    for (int m = 0; m < C::kMelBins; ++m) {
      const float center = frequencies[m + 1];
      const float band = center - frequencies[m];
      for (size_t k = 0; k < kFrequencies; ++k) {
        const float hz = static_cast<float>(k) * C::kSampleRate / C::kFft;
        const float slope = (hz - center) / band;
        filters[m * kFrequencies + k] =
            std::max(0.f, std::min(slope + 1.f, -slope + 1.f));
      }
    }
  }
};
Status Interrupted() {
  return Status::Fail("Speaker feature computation cancelled");
}
}  // namespace

Status ComputeSpeakerFeatures(std::span<const float> samples, int sample_rate,
                              const SpeakerFeatureLimits& limits,
                              SpeakerFeatures* out,
                              const std::function<bool()>& cancelled) {
  if (!out || sample_rate != C::kSampleRate)
    return Status::Fail("Speaker features require an owner and 16 kHz mono PCM");
  const auto interrupted = [&] { return cancelled && cancelled(); };
  if (interrupted()) return Interrupted();
  if (samples.empty() || samples.size() > limits.max_samples ||
      samples.size() > static_cast<size_t>(INT64_MAX - C::kFft))
    return Status::Fail("Speaker sample count exceeds the feature budget");
  const size_t frames = 1 + samples.size() / C::kHop;
  if (frames > limits.max_frames ||
      frames > std::numeric_limits<size_t>::max() / C::kMelBins / sizeof(float))
    return Status::Fail("Speaker frame count exceeds the feature budget");
  for (size_t i = 0; i < samples.size(); ++i) {
    if (!std::isfinite(samples[i]))
      return Status::Fail("Speaker PCM contains a non-finite sample");
    if ((i & 4095) == 0 && interrupted()) return Interrupted();
  }
  static const Tables tables;
  SpeakerFeatures result;
  result.frames = frames;
  try {
    result.mel.resize(frames * C::kMelBins);
  } catch (const std::bad_alloc&) {
    return Status::Fail("Speaker feature allocation failed");
  }
  std::array<float, C::kFft> windowed;
  std::array<float, kFrequencies> powers;
  float maximum = -std::numeric_limits<float>::infinity();
  for (size_t t = 0; t < frames; ++t) {
    if (interrupted()) return Interrupted();
    const int64_t first = static_cast<int64_t>(t * C::kHop) - C::kFft / 2;
    for (int n = 0; n < C::kFft; ++n) {
      const auto index = first + n;
      windowed[n] = index >= 0 && index < static_cast<int64_t>(samples.size())
                        ? samples[index] * tables.window[n]
                        : 0.f;
    }
    for (size_t k = 0; k < kFrequencies; ++k) {
      double real = 0, imaginary = 0;
      for (int n = 0; n < C::kFft; ++n) {
        real += windowed[n] * tables.real[k * C::kFft + n];
        imaginary += windowed[n] * tables.imaginary[k * C::kFft + n];
      }
      const auto r = static_cast<float>(real);
      const auto i = static_cast<float>(imaginary);
      powers[k] = r * r + i * i;
      if (!std::isfinite(powers[k]))
        return Status::Fail("Speaker power spectrum is non-finite");
    }
    for (int m = 0; m < C::kMelBins; ++m) {
      double sum = 0;
      for (size_t k = 0; k < kFrequencies; ++k)
        sum += static_cast<double>(tables.filters[m * kFrequencies + k]) *
               powers[k];
      const float energy = static_cast<float>(sum);
      const float value = 10.f * std::log10(std::max(energy, 1e-10f));
      if (!std::isfinite(value))
        return Status::Fail("Speaker filter energy is non-finite");
      result.mel[m * frames + t] = value;
      maximum = std::max(maximum, value);
    }
  }
  const float floor = maximum - 80.f;
  for (int m = 0; m < C::kMelBins; ++m) {
    if (interrupted()) return Interrupted();
    double sum = 0;
    for (size_t t = 0; t < frames; ++t) {
      auto& value = result.mel[m * frames + t];
      value = std::max(value, floor);
      sum += value;
    }
    const float mean = sum / frames;
    for (size_t t = 0; t < frames; ++t)
      result.mel[m * frames + t] -= mean;
  }
  if (interrupted()) return Interrupted();
  *out = std::move(result);
  return {};
}
}  // namespace q4t::audio
