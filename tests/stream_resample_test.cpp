#include "q4t/audio/stream_resample.h"

#include <algorithm>
#include <bit>
#include <cmath>
#include <limits>
#include <numbers>
#include <stdexcept>
#include <vector>

#include "q4t/test.h"

namespace {
using namespace q4t::audio;
std::vector<float> Convert(uint32_t rate, std::span<const float> input,
                           size_t chunk = 2048, size_t output_chunk = 2048,
                           uint32_t output_rate = 16000) {
  AsrStreamResampler stream(rate, std::max<size_t>(1, input.size()),
                             output_rate);
  std::vector<float> output, buffer(output_chunk);
  size_t position = 0;
  while (position < input.size()) {
    const auto part =
        input.subspan(position, std::min(chunk, input.size() - position));
    ResampleProgress progress;
    if (!stream.Process(part, buffer, &progress) ||
        (!progress.consumed && !progress.produced))
      throw std::runtime_error("resample process");
    position += progress.consumed;
    output.insert(output.end(), buffer.begin(),
                  buffer.begin() + progress.produced);
  }
  do {
    size_t produced;
    if (!stream.Finish(buffer, &produced))
      throw std::runtime_error("resample finish");
    output.insert(output.end(), buffer.begin(), buffer.begin() + produced);
  } while (!stream.Drained());
  if (stream.InputSamples() != input.size() ||
      stream.OutputSamples() != output.size())
    throw std::runtime_error("sample accounting");
  return output;
}
std::vector<float> Tone(uint32_t rate, double frequency) {
  std::vector<float> output(rate);
  for (size_t i = 0; i < output.size(); ++i)
    output[i] = .5 * std::sin(2 * std::numbers::pi * frequency * i / rate);
  return output;
}
}  // namespace

Q4T_TEST(stream_resample_identity_keeps_float_bits_and_no_lookahead) {
  const std::vector<float> input{0, -0.f, 1, -1, .2f, -.001f};
  const auto output = Convert(16000, input, 1, 1);
  Q4T_CHECK(output.size() == input.size());
  for (size_t i = 0; i < input.size(); ++i)
    Q4T_CHECK(std::bit_cast<uint32_t>(input[i]) ==
              std::bit_cast<uint32_t>(output[i]));
  AsrStreamResampler stream(16000, 1);
  float result;
  ResampleProgress progress;
  Q4T_CHECK(
      stream.Process(std::span(input).subspan(2, 1), {&result, 1}, &progress));
  Q4T_CHECK(progress.consumed == 1 && progress.produced == 1 && result == 1);
  return true;
}

Q4T_TEST(
    stream_resample_chunk_partition_and_output_backpressure_are_bit_exact) {
  std::vector<float> input(4001);
  for (size_t i = 0; i < input.size(); ++i)
    input[i] = .7 * std::sin(i * .314159) + .2 * std::cos(i * .7723);
  for (const uint32_t rate : {8000, 16000, 24000, 48000}) {
    const auto expected = Convert(rate, input);
    for (const size_t chunk : {1, 7, 47, 48, 49, 97, 129, 2048})
      for (const size_t output_chunk : {1, 31})
        Q4T_CHECK(Convert(rate, input, chunk, output_chunk) == expected);
  }
  return true;
}

Q4T_TEST(stream_resample_tiny_tail_has_exact_total_count_without_padding) {
  for (const uint32_t rate : {8000, 16000, 24000, 48000}) {
    for (size_t count = 0; count < 140; ++count) {
      const auto result = Convert(rate, std::vector<float>(count, .25f), 3, 2);
      Q4T_CHECK(result.size() == count * 16000 / rate);
      for (const auto value : result) Q4T_CHECK(std::isfinite(value));
    }
  }
  return true;
}

Q4T_TEST(stream_resample_impulse_keeps_fractional_time_origin_and_mass) {
  for (const uint32_t rate : {8000, 16000, 24000, 48000}) {
    std::vector<float> input(rate / 10);
    const size_t position = rate / 100 + 1;
    input[position] = 1;
    const auto result = Convert(rate, input, 17, 19);
    double mass = 0, moment = 0;
    for (size_t i = 0; i < result.size(); ++i) {
      mass += result[i];
      moment += double(i) * result[i];
    }
    Q4T_CHECK(std::abs(mass - 16000. / rate) < 2e-4);
    Q4T_CHECK(std::abs(moment / mass - double(position) * 16000 / rate) < .003);
  }
  return true;
}

Q4T_TEST(stream_resample_constant_signal_retains_unity_dc) {
  for (const uint32_t rate : {8000, 16000, 24000, 48000}) {
    const auto result =
        Convert(rate, std::vector<float>(rate / 4, .25f), 53, 23);
    for (size_t i = 128; i + 128 < result.size(); ++i)
      Q4T_CHECK(std::abs(result[i] - .25f) < 1e-7);
  }
  return true;
}

Q4T_TEST(
    stream_resample_voice_band_survives_and_high_frequencies_do_not_alias) {
  for (const uint32_t rate : {24000, 48000}) {
    for (const double frequency : {1000., 11000.}) {
      const auto output = Convert(rate, Tone(rate, frequency), 373, 97);
      double energy = 0;
      for (size_t i = 128; i + 128 < output.size(); ++i)
        energy += double(output[i]) * output[i];
      const auto rms = std::sqrt(energy / (output.size() - 256));
      Q4T_CHECK(frequency == 1000. ? std::abs(rms - std::sqrt(.125)) < .001
                                   : rms < .0001);
    }
  }
  const auto up = Convert(8000, Tone(8000, 1000));
  for (const double frequency : {1000., 7000.}) {
    double sine = 0, cosine = 0;
    for (size_t i = 128; i + 128 < up.size(); ++i) {
      const auto phase = 2 * std::numbers::pi * frequency * i / 16000;
      sine += up[i] * std::sin(phase);
      cosine += up[i] * std::cos(phase);
    }
    const double amplitude = 2 * std::hypot(sine, cosine) / (up.size() - 256);
    Q4T_CHECK(frequency == 1000. ? std::abs(amplitude - .5) < .001
                                 : amplitude < .0001);
  }
  return true;
}

Q4T_TEST(stream_resample_bad_input_and_capacity_reject_before_mutation) {
  AsrStreamResampler stream(24000, 100);
  std::vector<float> output(100, 123), input(99, .5f);
  for (const float bad : {2.f, std::numeric_limits<float>::infinity(),
                          std::numeric_limits<float>::quiet_NaN()}) {
    input.back() = bad;
    ResampleProgress progress{7, 8};
    Q4T_CHECK(!stream.Process(input, output, &progress));
    Q4T_CHECK(!progress.consumed && !progress.produced &&
              !stream.InputSamples());
    Q4T_CHECK(output == std::vector<float>(100, 123));
  }
  ResampleProgress progress;
  input.assign(101, .5f);
  Q4T_CHECK(!stream.Process(input, output, &progress) &&
            !stream.InputSamples());
  input.assign(99, .5f);
  Q4T_CHECK(!stream.Process(input, input, &progress) && !stream.InputSamples());
  Q4T_CHECK(stream.Process(input, {}, &progress) && !stream.InputSamples());
  Q4T_CHECK(stream.Process(input, output, &progress) &&
            progress.consumed == 99);
  Q4T_CHECK(!stream.Process(std::span(input).first(2), output, &progress));
  Q4T_CHECK(stream.InputSamples() == 99);
  return true;
}

Q4T_TEST(stream_resample_finish_and_cancel_cannot_invent_completion) {
  AsrStreamResampler stream(24000, 100), cancelled(24000, 100);
  std::vector<float> input(50, .2f), output(2);
  ResampleProgress progress;
  Q4T_CHECK(stream.Process(input, output, &progress));
  Q4T_CHECK(!stream.Drained());
  size_t produced;
  Q4T_CHECK(stream.Finish({}, &produced) && !produced && !stream.Drained());
  Q4T_CHECK(!stream.Process(input, output, &progress));
  while (!stream.Drained()) Q4T_CHECK(stream.Finish(output, &produced));
  Q4T_CHECK(stream.OutputSamples() == stream.InputSamples() * 2 / 3);
  Q4T_CHECK(stream.Finish(output, &produced) && !produced);
  Q4T_CHECK(cancelled.Process(input, output, &progress));
  cancelled.Cancel();
  Q4T_CHECK(cancelled.Cancelled() && !cancelled.Drained());
  Q4T_CHECK(!cancelled.Finish(output, &produced) && !produced);
  Q4T_CHECK(!cancelled.Process({}, output, &progress));
  return true;
}

Q4T_TEST(stream_resample_fixed_storage_and_configuration_bounds) {
  static_assert(sizeof(AsrStreamResampler) < 4096);
  for (const uint32_t rate : {0u, 44100u, 96000u, UINT32_MAX})
    Q4T_CHECK(!AsrStreamResampler(rate, 1).Valid());
  for (const uint32_t rate : {8000, 16000, 24000, 48000}) {
    Q4T_CHECK(!AsrStreamResampler(rate, 0).Valid());
    Q4T_CHECK(!AsrStreamResampler(rate, uint64_t(rate) * 3600 + 1).Valid());
    Q4T_CHECK(AsrStreamResampler(rate, uint64_t(rate) * 3600).Valid());
  }
  return true;
}

Q4T_TEST(stream_resample_export_rates_preserve_partition_and_tail) {
  for (const uint32_t out_rate : {8000, 24000}) {
    for (const uint32_t rate : {8000, 16000, 24000, 48000}) {
      const auto input = Tone(rate, 1000);
      const auto expected = Convert(rate, input, 2048, 2048, out_rate);
      Q4T_CHECK(expected.size() == out_rate);
      Q4T_CHECK(Convert(rate, input, 1, 3, out_rate) == expected);
      Q4T_CHECK(Convert(rate, input, 511, 1, out_rate) == expected);
      for (size_t count = 0; count < 101; ++count) {
        const auto result =
            Convert(rate, std::span(input).first(count), 7, 2, out_rate);
        Q4T_CHECK(result.size() == count * out_rate / rate);
      }
    }
  }
  return true;
}

Q4T_TEST(stream_resample_export_matches_independent_centered_convolution) {
  // Direct long-double convolution over the whole signal, no phase table,
  // ring, lookahead or streamed state shared with the implementation.
  const auto input = Tone(16000, 1700);
  for (const uint32_t out_rate : {8000, 24000}) {
    const auto output = Convert(16000, input, 13, 7, out_rate);
    for (const size_t at : {0, 1, 2, 3, 47, 48, 49, 101, 3001, 7999}) {
      const long double position = (long double)at * 16000 / out_rate;
      const auto center = static_cast<int64_t>(std::floor(position));
      long double sum = 0, weights = 0;
      const long double cutoff = .45L * std::min(out_rate, 16000u) / 16000;
      for (int64_t source = center - 48; source <= center + 48; ++source) {
        const long double x = source - position;
        if (std::abs(x) >= 48) continue;
        const auto angle = 2 * std::numbers::pi_v<long double> * cutoff * x;
        const auto sinc = x == 0 ? 1.L : std::sin(angle) / angle;
        const auto window = .42L +
                            .5L * std::cos(std::numbers::pi_v<long double> *
                                           x / 48) +
                            .08L * std::cos(2 * std::numbers::pi_v<long double> *
                                            x / 48);
        const auto weight = 2 * cutoff * sinc * window;
        weights += weight;
        if (source >= 0 && uint64_t(source) < input.size())
          sum += input[source] * weight;
      }
      Q4T_CHECK(std::abs(output[at] - sum / weights) < 2e-7);
    }
  }
  return true;
}

Q4T_TEST(stream_resample_export_accepts_finite_overshoot_only_when_explicit) {
  AsrStreamResampler strict(16000, 1), export_pcm(16000, 1, 16000, false);
  const float value = 2;
  float out = 123;
  ResampleProgress progress;
  Q4T_CHECK(!strict.Process({&value, 1}, {&out, 1}, &progress));
  Q4T_CHECK(!strict.InputSamples() && out == 123);
  Q4T_CHECK(export_pcm.Process({&value, 1}, {&out, 1}, &progress));
  Q4T_CHECK(out == value && progress.consumed == 1 && progress.produced == 1);
  AsrStreamResampler invalid(16000, 1, 24000, false);
  const float nonfinite = std::numeric_limits<float>::infinity();
  Q4T_CHECK(!invalid.Process({&nonfinite, 1}, {&out, 1}, &progress));
  Q4T_CHECK(!invalid.InputSamples());
  for (const uint32_t rate : {0u, 1u, 44100u, 48000u, UINT32_MAX})
    Q4T_CHECK(!AsrStreamResampler(16000, 1, rate).Valid());
  return true;
}
