#include "q4t/audio/tts_speaker_features.h"

#include <algorithm>
#include <array>
#include <cmath>
#include <cstdint>
#include <new>
#include <numbers>
#include <utility>

namespace q4t::audio {
namespace {
constexpr int kFft = 1024, kHop = 256, kBins = 128, kRate = 24000;
constexpr int kFrequencies = kFft / 2 + 1, kPadding = (kFft - kHop) / 2;
constexpr size_t kMaxSamples = 1440000;
struct Tables {
  std::array<double, kFft> window;
  std::array<double, kFrequencies * kFft> real, imaginary;
  std::array<float, kBins * kFrequencies> filters;
  Tables() {
    constexpr double pi = std::numbers::pi_v<double>;
    const double phase_step = 2 * pi / kFft;
    for (int n = 0; n < kFft; ++n)
      window[n] = 0.5 - 0.5 * std::cos(n * phase_step);
    for (int k = 0; k < kFrequencies; ++k)
      for (int n = 0; n < kFft; ++n) {
        const double phase = 2 * pi * k * n / kFft;
        real[k * kFft + n] = std::cos(phase);
        imaginary[k * kFft + n] = -std::sin(phase);
      }
    // Independent standard Slaney equations. Librosa casts the triangle to
    // float32 before multiplying by its area normalization, also in float32.
    const double log_step = std::log(6.4) / 27;
    const double maximum = 15 + std::log(12.0) / log_step;
    std::array<double, kBins + 2> frequencies;
    for (int i = 0; i < kBins + 2; ++i) {
      const double mel = maximum * i / (kBins + 1);
      frequencies[i] =
          mel < 15 ? mel * (200.0 / 3) : 1000 * std::exp(log_step * (mel - 15));
    }
    for (int m = 0; m < kBins; ++m) {
      const double left = frequencies[m], center = frequencies[m + 1];
      const double right = frequencies[m + 2];
      for (int k = 0; k < kFrequencies; ++k) {
        const double hz = static_cast<double>(k) * kRate / kFft;
        const float triangle =
            std::max(0.0, std::min((hz - left) / (center - left),
                                   (right - hz) / (right - center)));
        filters[m * kFrequencies + k] = triangle * (2 / (right - left));
      }
    }
  }
};
struct Scratch {
  std::array<double, kFft> windowed;
  std::array<double, kFrequencies> magnitudes;
};
Status Interrupted() { return Status::Fail("TTS speaker features cancelled"); }
}  // namespace

size_t TtsSpeakerFeatureFixedBytes() {
  // Includes temporary filter-frequency construction as well as frame scratch.
  return sizeof(Tables) + sizeof(Scratch) + sizeof(double) * (kBins + 2);
}

Status ComputeTtsSpeakerFeatures(std::span<const float> samples,
                                 int sample_rate,
                                 const TtsSpeakerFeatureLimits& limits,
                                 TtsSpeakerFeatures* out,
                                 const std::function<bool()>& cancelled) {
  if (!out || sample_rate != kRate)
    return Status::Fail("TTS speaker features require an owner and 24 kHz PCM");
  const auto interrupted = [&] { return cancelled && cancelled(); };
  if (interrupted()) return Interrupted();
  if (samples.size() <= kPadding || samples.size() > kMaxSamples ||
      samples.size() > limits.max_samples)
    return Status::Fail("TTS speaker sample count exceeds the feature budget");
  const size_t frames = samples.size() / kHop;
  if (frames > limits.max_frames)
    return Status::Fail("TTS speaker frame count exceeds the feature budget");
  const size_t bytes = (samples.size() + frames * kBins) * sizeof(float) +
                       TtsSpeakerFeatureFixedBytes();
  if (bytes > limits.max_bytes)
    return Status::Fail("TTS speaker features exceed the physical RAM budget");
  for (size_t i = 0; i < samples.size(); ++i) {
    if (!std::isfinite(samples[i]) || samples[i] < -1 || samples[i] > 1)
      return Status::Fail("TTS speaker PCM must be finite and normalized");
    if ((i & 4095) == 0 && interrupted()) return Interrupted();
  }
  static const Tables tables;
  TtsSpeakerFeatures result;
  result.frames = frames;
  try {
    result.mel.resize(frames * kBins);
  } catch (const std::bad_alloc&) {
    return Status::Fail("TTS speaker feature allocation failed");
  }
  Scratch scratch;
  for (size_t t = 0; t < frames; ++t) {
    if (interrupted()) return Interrupted();
    const int64_t first = static_cast<int64_t>(t * kHop) - kPadding;
    for (int n = 0; n < kFft; ++n) {
      int64_t index = first + n;
      if (index < 0) index = -index;
      if (index >= static_cast<int64_t>(samples.size()))
        index = 2 * static_cast<int64_t>(samples.size()) - 2 - index;
      scratch.windowed[n] = samples[index] * tables.window[n];
    }
    for (int k = 0; k < kFrequencies; ++k) {
      double real = 0, imaginary = 0;
      for (int n = 0; n < kFft; ++n) {
        real += scratch.windowed[n] * tables.real[k * kFft + n];
        imaginary += scratch.windowed[n] * tables.imaginary[k * kFft + n];
      }
      scratch.magnitudes[k] =
          std::sqrt(real * real + imaginary * imaginary + 1e-9);
    }
    for (int m = 0; m < kBins; ++m) {
      double sum = 0;
      for (int k = 0; k < kFrequencies; ++k)
        sum += static_cast<double>(tables.filters[m * kFrequencies + k]) *
               scratch.magnitudes[k];
      const float value = std::log(std::max(sum, 1e-5));
      if (!std::isfinite(value))
        return Status::Fail("TTS speaker feature computation is nonfinite");
      result.mel[m * frames + t] = value;
    }
  }
  if (interrupted()) return Interrupted();
  *out = std::move(result);
  return {};
}
}  // namespace q4t::audio
