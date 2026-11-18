#include "q4t/audio/separator_resample.h"
#include "q4t/test.h"

#include <algorithm>
#include <cmath>
#include <filesystem>
#include <fstream>
#include <limits>
#include <numbers>
#include <stdexcept>

namespace {
using namespace q4t::audio;
std::vector<float> Fixture(const std::string& name) {
  const auto path = std::filesystem::path(__FILE__).parent_path() /
                    "fixtures/separator/resample" / (name + ".f32");
  const auto size = std::filesystem::file_size(path);
  if (size % 4 || size > 32000) throw std::runtime_error("resample fixture");
  std::vector<float> result(size / 4);
  std::ifstream file(path, std::ios::binary);
  file.read(reinterpret_cast<char*>(result.data()), size);
  if (!file) throw std::runtime_error("resample fixture read");
  return result;
}
bool Close(std::span<const float> a, std::span<const float> b) {
  if (a.size() != b.size()) return false;
  for (size_t i = 0; i < a.size(); ++i)
    if (!std::isfinite(a[i]) || std::abs(double(a[i]) - b[i]) > 2e-7)
      return false;
  return true;
}
}  // namespace

Q4T_TEST(separator_normalization_preserves_waveform_and_aliasing) {
  const std::vector<float> input{-2.f, 4.f, 1.f, 0.f, -4.f};
  const std::vector<float> expected{-.5f, 1.f, .25f, 0.f, -1.f};
  std::vector<float> output{123};
  Q4T_CHECK(NormalizeSeparation(input, 480000, &output));
  Q4T_CHECK(output == expected && input[1] == 4.f);
  auto alias = input;
  Q4T_CHECK(NormalizeSeparation(alias, 480000, &alias));
  Q4T_CHECK(alias == expected);
  return true;
}

Q4T_TEST(separator_normalization_handles_silence_and_finite_extremes) {
  std::vector<float> silent(480000), output;
  Q4T_CHECK(NormalizeSeparation(silent, 480000, &output));
  Q4T_CHECK(output == silent);
  for (float value : {std::numeric_limits<float>::denorm_min(),
                      std::numeric_limits<float>::max()}) {
    const std::vector<float> input{value, -value, 0};
    Q4T_CHECK(NormalizeSeparation(input, 480000, &output));
    Q4T_CHECK(output == std::vector<float>({1, -1, 0}));
  }
  return true;
}

Q4T_TEST(separator_normalization_rejects_bad_input_atomically) {
  std::vector<float> output{123};
  for (const auto& input : std::vector<std::vector<float>>{
           {},
           std::vector<float>(480001),
           {std::numeric_limits<float>::quiet_NaN()},
           {std::numeric_limits<float>::infinity()}}) {
    Q4T_CHECK(!NormalizeSeparation(input, 480000, &output));
    Q4T_CHECK(output == std::vector<float>({123}));
  }
  const std::vector<float> input{1, 2};
  Q4T_CHECK(!NormalizeSeparation(input, 1, &output));
  Q4T_CHECK(!NormalizeSeparation(input, 480000, nullptr));
  Q4T_CHECK(output == std::vector<float>({123}));
  return true;
}

Q4T_TEST(separator_normalization_cancellation_keeps_owner) {
  std::vector<float> input(1024, 2.f);
  for (int stop : {1, 3, 6, 9}) {
    int calls = 0;
    Q4T_CHECK(!NormalizeSeparation(input, 480000, &input,
                                   [&] { return ++calls == stop; }));
    Q4T_CHECK(input == std::vector<float>(1024, 2.f));
  }
  return true;
}

Q4T_TEST(separator_resampling_matches_independent_full_convolution) {
  for (const auto* name : {"silence", "impulse", "tone", "noise"}) {
    const auto input = Fixture(std::string(name) + "-input");
    std::vector<float> down, restored;
    Q4T_CHECK(DownsampleSeparation(input, 480000, &down));
    Q4T_CHECK(Close(down, Fixture(std::string(name) + "-down")));
    Q4T_CHECK(RestoreSeparation(down, input.size(), 480000, &restored));
    Q4T_CHECK(Close(restored, Fixture(std::string(name) + "-restored")));
  }
  return true;
}

Q4T_TEST(separator_resampling_filters_alias_and_preserves_voice_band) {
  for (double frequency : {440., 6000.}) {
    std::vector<float> input(16000), down;
    for (size_t i = 0; i < input.size(); ++i)
      input[i] = std::sin(2 * std::numbers::pi * frequency * i / 16000.);
    Q4T_CHECK(DownsampleSeparation(input, 16000, &down));
    double square = 0;
    for (size_t i = 64; i + 64 < down.size(); ++i)
      square += double(down[i]) * down[i];
    const double rms = std::sqrt(square / (down.size() - 128));
    Q4T_CHECK(frequency == 440. ? (rms > .70 && rms < .71) : rms < .001);
  }
  std::vector<float> dc(1024, .25), down, restored;
  Q4T_CHECK(DownsampleSeparation(dc, 1024, &down));
  Q4T_CHECK(RestoreSeparation(down, dc.size(), 1024, &restored));
  for (size_t i = 64; i + 64 < dc.size(); ++i)
    Q4T_CHECK(std::abs(restored[i] - .25f) < .0001f);
  return true;
}

Q4T_TEST(separator_resampling_preserves_exact_duration_and_time_origin) {
  for (size_t size : {size_t{1}, size_t{2}, size_t{17}, size_t{64001},
                      kSeparationMaxInputSamples}) {
    std::vector<float> input(size), down, restored;
    Q4T_CHECK(DownsampleSeparation(input, size, &down));
    Q4T_CHECK(down.size() == (size + 1) / 2);
    Q4T_CHECK(RestoreSeparation(down, size, size, &restored));
    Q4T_CHECK(restored.size() == size);
    Q4T_CHECK(std::all_of(restored.begin(), restored.end(),
                          [](float x) { return x == 0; }));
  }
  std::vector<float> input(4001), down, restored;
  input[1000] = 1;
  Q4T_CHECK(DownsampleSeparation(input, input.size(), &down));
  Q4T_CHECK(std::max_element(down.begin(), down.end()) - down.begin() == 500);
  Q4T_CHECK(RestoreSeparation(down, input.size(), input.size(), &restored));
  Q4T_CHECK(std::max_element(restored.begin(), restored.end()) -
                restored.begin() ==
            1000);
  return true;
}

Q4T_TEST(separator_resampling_rejects_budget_and_nonfinite_atomically) {
  const std::vector<float> original{123};
  std::vector<float> out = original, input(16, .5);
  Q4T_CHECK(!DownsampleSeparation({}, 16, &out) && out == original);
  Q4T_CHECK(!DownsampleSeparation(input, 15, &out) && out == original);
  Q4T_CHECK(!DownsampleSeparation(input, 16, nullptr));
  for (float x : {std::numeric_limits<float>::infinity(),
                  std::numeric_limits<float>::quiet_NaN()}) {
    input[7] = x;
    Q4T_CHECK(!DownsampleSeparation(input, 16, &out) && out == original);
    Q4T_CHECK(!RestoreSeparation(input, 32, 32, &out) && out == original);
  }
  input.assign(kSeparationMaxInputSamples + 1, 0);
  Q4T_CHECK(!DownsampleSeparation(input, SIZE_MAX, &out) && out == original);
  return true;
}

Q4T_TEST(separator_resampling_cancel_before_during_and_at_commit) {
  std::vector<float> input(8000, .25);
  for (int threshold : {1, 8, 18}) {
    int calls = 0;
    std::vector<float> output{123};
    Q4T_CHECK(!DownsampleSeparation(input, 8000, &output,
                                    [&] { return ++calls >= threshold; }));
    Q4T_CHECK(output == std::vector<float>({123}));
  }
  for (int threshold : {1, 8, 34}) {
    int calls = 0;
    std::vector<float> output{123};
    Q4T_CHECK(!RestoreSeparation(std::span(input).first(4000), 8000, 8000,
                                 &output,
                                 [&] { return ++calls >= threshold; }));
    Q4T_CHECK(output == std::vector<float>({123}));
  }
  return true;
}

Q4T_TEST(separator_restoration_rejects_wrong_original_shape_and_limits) {
  std::vector<float> input(8), output{123};
  for (size_t size : {size_t{0}, size_t{14}, size_t{17}, SIZE_MAX}) {
    Q4T_CHECK(!RestoreSeparation(input, size, SIZE_MAX, &output));
    Q4T_CHECK(output == std::vector<float>({123}));
  }
  Q4T_CHECK(!RestoreSeparation(input, 16, 15, &output));
  Q4T_CHECK(!RestoreSeparation(input, 16, 16, nullptr));
  Q4T_CHECK(RestoreSeparation(input, 15, 15, &output) && output.size() == 15);
  return true;
}
