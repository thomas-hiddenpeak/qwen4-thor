#include "q4t/audio/features.h"
#include "q4t/test.h"

#include <algorithm>
#include <bit>
#include <cmath>
#include <cstdint>
#include <filesystem>
#include <fstream>
#include <limits>
#include <stdexcept>
#include <vector>

namespace {
using q4t::audio::AsrFeatures;
using q4t::audio::ComputeAsrFeatures;

std::vector<float> Fixture(const std::string& name) {
  const auto path = std::filesystem::path(__FILE__).parent_path() /
                    "fixtures/asr/features" / name;
  std::ifstream file(path, std::ios::binary | std::ios::ate);
  const auto length = file.tellg();
  if (!file || length <= 0 || length > 1024 * 1024 || length % 4)
    throw std::runtime_error("ASR feature fixture size/file");
  std::vector<unsigned char> bytes(static_cast<size_t>(length));
  file.seekg(0);
  file.read(reinterpret_cast<char*>(bytes.data()), bytes.size());
  if (!file) throw std::runtime_error("ASR feature fixture truncated");
  std::vector<float> values(bytes.size() / 4);
  for (size_t i = 0; i < values.size(); ++i) {
    uint32_t bits = 0;
    for (size_t byte = 0; byte < 4; ++byte)
      bits |= static_cast<uint32_t>(bytes[4 * i + byte]) << (8 * byte);
    values[i] = std::bit_cast<float>(bits);
  }
  return values;
}

bool Golden(const std::string& name, size_t expected_samples) {
  const auto samples = Fixture(name + "-pcm.f32");
  const auto expected = Fixture(name + "-mel.f32");
  Q4T_CHECK(samples.size() == expected_samples);
  AsrFeatures actual;
  Q4T_CHECK(ComputeAsrFeatures(samples, 16000, {}, &actual));
  Q4T_CHECK(actual.frames == samples.size() / 160);
  Q4T_CHECK(actual.mel.size() == expected.size());
  double maximum = 0;
  for (size_t i = 0; i < expected.size(); ++i) {
    Q4T_CHECK(std::isfinite(actual.mel[i]));
    maximum = std::max(maximum, std::abs(
        static_cast<double>(actual.mel[i]) - expected[i]));
  }
  std::printf("  %s: %zu frames, maximum absolute error %.9g\n",
              name.c_str(), actual.frames, maximum);
  Q4T_CHECK(maximum <= 1e-5);
  return true;
}
}  // namespace

Q4T_TEST(asr_features_silence_matches_official_log_floor) {
  return Golden("silence", 1600);
}
Q4T_TEST(asr_features_boundary_impulses_match_reflect_padding) {
  return Golden("impulse", 321);
}
Q4T_TEST(asr_features_tone_matches_periodic_hann_and_slaney_filters) {
  return Golden("tone", 8000);
}
Q4T_TEST(asr_features_mixture_matches_log_dynamic_range) {
  return Golden("mixture", 16000);
}
Q4T_TEST(asr_features_noise_matches_full_official_matrix) {
  return Golden("noise", 2563);
}
Q4T_TEST(asr_features_partial_hop_matches_official_frame_count) {
  return Golden("tail", 16179);
}

Q4T_TEST(asr_features_reject_invalid_input_and_enforce_allocation_bounds) {
  const AsrFeatures original{12, {3.0f, 4.0f}};
  AsrFeatures output = original;
  const auto unchanged = [&] {
    return output.frames == original.frames && output.mel == original.mel;
  };
  std::vector<float> samples(160, 0.0f);
  Q4T_CHECK(!ComputeAsrFeatures(samples, 8000, {}, &output) && unchanged());
  Q4T_CHECK(!ComputeAsrFeatures({}, 16000, {}, &output) && unchanged());
  Q4T_CHECK(!ComputeAsrFeatures(std::span(samples).first(159), 16000, {},
                                &output) && unchanged());
  Q4T_CHECK(!ComputeAsrFeatures(samples, 16000, {.max_samples = 159},
                                &output) && unchanged());
  Q4T_CHECK(!ComputeAsrFeatures(samples, 16000, {.max_frames = 0},
                                &output) && unchanged());
  for (float invalid : {std::numeric_limits<float>::infinity(),
                         std::numeric_limits<float>::quiet_NaN()}) {
    samples[157] = invalid;
    Q4T_CHECK(!ComputeAsrFeatures(samples, 16000, {}, &output) && unchanged());
  }
  samples[157] = 0;
  Q4T_CHECK(!ComputeAsrFeatures(samples, 16000, {}, nullptr));
  Q4T_CHECK(ComputeAsrFeatures(samples, 16000, {}, &output));
  Q4T_CHECK(output.frames == 1 && output.mel.size() == 128);
  Q4T_CHECK(std::all_of(output.mel.begin(), output.mel.end(),
                        [](float x) { return x == -1.5f; }));
  // Maximum configured input, not a larger implicit context or padded tail.
  samples.assign(480000, 0);
  Q4T_CHECK(ComputeAsrFeatures(samples, 16000, {}, &output));
  Q4T_CHECK(output.frames == 3000 && output.mel.size() == 128 * 3000);
  const auto previous = output.mel;
  samples.push_back(0);
  Q4T_CHECK(!ComputeAsrFeatures(samples, 16000, {}, &output));
  Q4T_CHECK(output.frames == 3000 && output.mel == previous);
  return true;
}

Q4T_TEST(asr_features_cancel_during_compute_preserves_output_and_reuses_tables) {
  const auto samples = Fixture("mixture-pcm.f32");
  AsrFeatures output{12, {3.0f, 4.0f}};
  int checkpoints = 0;
  Q4T_CHECK(!ComputeAsrFeatures(samples, 16000, {}, &output,
                                [&] { return ++checkpoints == 15; }));
  Q4T_CHECK(checkpoints == 15);
  Q4T_CHECK(output.frames == 12);
  Q4T_CHECK(output.mel == std::vector<float>({3, 4}));
  Q4T_CHECK(ComputeAsrFeatures(samples, 16000, {}, &output));
  Q4T_CHECK(output.frames == 100 && output.mel.size() == 12800);
  return true;
}
