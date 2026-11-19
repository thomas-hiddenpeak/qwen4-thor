#include "q4t/audio/noise_reduction.h"

#include <algorithm>
#include <cmath>
#include <complex>
#include <limits>
#include <numbers>
#include <stdexcept>
#include <vector>

#include "../src/audio/noise_fft.h"
#include "q4t/test.h"

namespace {
using namespace q4t::audio;
std::vector<float> Reduce(std::span<const float> input, NoiseReduction mode,
                          size_t chunk = 1024, size_t room = 1024) {
  NoiseStream stream(mode, input.size());
  std::vector<float> result;
  std::array<float, 1024> output;
  for (size_t at = 0; at < input.size();) {
    ResampleProgress p;
    if (!stream.Process(input.subspan(at, std::min(chunk, input.size() - at)),
                        std::span(output).first(room), &p) ||
        (p.consumed == 0 && p.produced == 0))
      throw std::runtime_error("noise fixture progress");
    at += p.consumed;
    result.insert(result.end(), output.begin(), output.begin() + p.produced);
  }
  for (size_t loops = 0; !stream.Drained(); ++loops) {
    size_t count = 0;
    if (loops > input.size() + 2 ||
        !stream.Finish(std::span(output).first(room), &count))
      throw std::runtime_error("noise fixture drain");
    result.insert(result.end(), output.begin(), output.begin() + count);
  }
  if (result.size() != input.size() || stream.InputSamples() != input.size() ||
      stream.OutputSamples() != input.size())
    throw std::runtime_error("noise fixture exact extent");
  return result;
}
double Tone(size_t i, double hz) {
  return std::sin(2 * std::numbers::pi * hz * double(i) / 16000);
}
double Projection(std::span<const float> samples, size_t begin, size_t end,
                  double hz) {
  double sum = 0;
  for (size_t i = begin; i < end; ++i) sum += samples[i] * Tone(i, hz);
  return 2 * sum / (end - begin);
}
}  // namespace

Q4T_TEST(noise_fft_matches_independent_direct_complex_dft_and_inverse) {
  using namespace q4t::audio::detail;
  for (int kind = 0; kind < 3; ++kind) {
    NoiseSpectrum source;
    for (size_t i = 0; i < source.size(); ++i) {
      source[i] = kind == 0 ? std::complex<double>(i == 7 ? 1 : 0)
                  : kind == 1
                      ? std::complex<double>(Tone(i, 125), .25)
                      : std::complex<double>(double((i * 37) % 101) / 101,
                                             double(i % 11) / 11);
    }
    auto transformed = source;
    NoiseFft(&transformed, false);
    for (size_t k = 0; k < source.size(); ++k) {
      std::complex<double> expected;
      for (size_t n = 0; n < source.size(); ++n) {
        const double phase = -2 * std::numbers::pi * double(k * n) / 512;
        expected +=
            source[n] * std::complex<double>(std::cos(phase), std::sin(phase));
      }
      Q4T_CHECK(std::abs(expected - transformed[k]) < 1e-9);
    }
    NoiseFft(&transformed, true);
    for (size_t n = 0; n < source.size(); ++n)
      Q4T_CHECK(std::abs(source[n] - transformed[n]) < 1e-12);
  }
  return true;
}

Q4T_TEST(noise_stream_partition_and_backpressure_are_bit_exact) {
  std::vector<float> source(4099);
  for (size_t i = 0; i < source.size(); ++i)
    source[i] = float(.1 * Tone(i, 125) + .4 * Tone(i, 1000));
  for (auto mode : {NoiseReduction::kNearField, NoiseReduction::kFarField}) {
    const auto expected = Reduce(source, mode);
    for (size_t chunk : {1, 17, 255, 256, 513, 1024})
      for (size_t room : {1, 5, 256, 1024})
        Q4T_CHECK(Reduce(source, mode, chunk, room) == expected);
  }
  return true;
}

Q4T_TEST(noise_stream_bypass_exact_and_finish_never_adds_samples) {
  for (size_t size : {1, 2, 255, 256, 257, 511, 512, 513, 1025}) {
    std::vector<float> source(size);
    for (size_t i = 0; i < size; ++i) source[i] = float(Tone(i, 1000));
    const auto bypass = Reduce(source, NoiseReduction::kOff, 17, 5);
    Q4T_CHECK(bypass == source);
    for (auto mode : {NoiseReduction::kNearField, NoiseReduction::kFarField}) {
      const auto result = Reduce(source, mode, 17, 5);
      Q4T_CHECK(result.size() == source.size());
      Q4T_CHECK(std::all_of(result.begin(), result.end(), [](float v) {
        return std::isfinite(v) && std::abs(v) <= 1;
      }));
    }
  }
  return true;
}

Q4T_TEST(noise_stream_stationary_hum_attenuation_and_speech_tone_retention) {
  std::vector<float> source(32000);
  for (size_t i = 0; i < source.size(); ++i)
    source[i] =
        float(.1 * Tone(i, 125) + (i >= 16000 ? .3 * Tone(i, 1000) : 0));
  const auto near = Reduce(source, NoiseReduction::kNearField);
  const auto far = Reduce(source, NoiseReduction::kFarField);
  const double input_noise = std::abs(Projection(source, 8192, 15872, 125));
  const double near_noise = std::abs(Projection(near, 8192, 15872, 125));
  const double far_noise = std::abs(Projection(far, 8192, 15872, 125));
  Q4T_CHECK(near_noise < input_noise * .5 && near_noise < far_noise);
  Q4T_CHECK(far_noise < input_noise * .65);
  const double original = Projection(source, 16896, 24576, 1000);
  Q4T_CHECK(Projection(near, 16896, 24576, 1000) > original * .8);
  Q4T_CHECK(Projection(far, 16896, 24576, 1000) > original * .8);
  return true;
}

Q4T_TEST(noise_stream_silence_and_connection_state_do_not_leak) {
  const std::vector<float> quiet(1025);
  Q4T_CHECK(Reduce(quiet, NoiseReduction::kNearField) == quiet);
  std::vector<float> source(16000, .4f);
  const auto first = Reduce(source, NoiseReduction::kNearField);
  Q4T_CHECK(Reduce(quiet, NoiseReduction::kFarField) == quiet);
  Q4T_CHECK(Reduce(source, NoiseReduction::kNearField) == first);
  return true;
}

Q4T_TEST(noise_stream_invalid_input_budget_and_cancel_never_drain_success) {
  NoiseStream stream(NoiseReduction::kNearField, 512);
  std::array<float, 513> input{};
  std::array<float, 512> output;
  output.fill(.123f);
  ResampleProgress p;
  Q4T_CHECK(!stream.Process(input, output, &p));
  input[0] = std::numeric_limits<float>::quiet_NaN();
  Q4T_CHECK(!stream.Process(std::span(input).first(1), output, &p));
  input[0] = 1.001f;
  Q4T_CHECK(!stream.Process(std::span(input).first(1), output, &p));
  Q4T_CHECK(stream.InputSamples() == 0 && stream.OutputSamples() == 0);
  Q4T_CHECK(std::all_of(output.begin(), output.end(),
                        [](float v) { return v == .123f; }));
  input[0] = 0;
  Q4T_CHECK(!stream.Process(std::span(input).first(512),
                            std::span(input).first(512), &p));
  Q4T_CHECK(stream.Process(std::span(input).first(512), output, &p));
  Q4T_CHECK(p.consumed == 512 && stream.InputSamples() == 512);
  stream.Cancel();
  size_t count = 0;
  Q4T_CHECK(!stream.Finish(output, &count) && !stream.Drained());
  Q4T_CHECK(!stream.Process({}, output, &p));
  NoiseStream empty(NoiseReduction::kNearField, 1);
  Q4T_CHECK(empty.Finish({}, &count) && empty.Drained());
  Q4T_CHECK(!empty.Process({}, output, &p));
  NoiseStream invalid(static_cast<NoiseReduction>(99), 1);
  Q4T_CHECK(!invalid.Valid() && !invalid.Finish(output, &count));
  return true;
}
