#include "q4t/audio/speaker_features.h"
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
using q4t::audio::SpeakerFeatures;
using q4t::audio::ComputeSpeakerFeatures;

std::vector<float> Fixture(const std::string& name) {
  const auto path = std::filesystem::path(__FILE__).parent_path() /
                    "fixtures/speaker/features" / name;
  std::ifstream file(path, std::ios::binary | std::ios::ate);
  const auto length = file.tellg();
  if (!file || length <= 0 || length > 1024 * 1024 || length % 4)
    throw std::runtime_error("Speaker feature fixture size/file");
  std::vector<unsigned char> bytes(static_cast<size_t>(length));
  file.seekg(0);
  file.read(reinterpret_cast<char*>(bytes.data()), bytes.size());
  if (!file) throw std::runtime_error("Speaker feature fixture truncated");
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
  SpeakerFeatures actual;
  Q4T_CHECK(ComputeSpeakerFeatures(samples, 16000, {}, &actual));
  Q4T_CHECK(actual.frames == 1 + samples.size() / 160);
  Q4T_CHECK(actual.mel.size() == expected.size());
  double maximum = 0;
  for (size_t i = 0; i < expected.size(); ++i) {
    Q4T_CHECK(std::isfinite(actual.mel[i]));
    maximum = std::max(maximum, std::abs(
        static_cast<double>(actual.mel[i]) - expected[i]));
  }
  std::printf("  %s: %zu frames, maximum absolute error %.9g\n",
              name.c_str(), actual.frames, maximum);
  Q4T_CHECK(maximum <= 0.002);
  return true;
}
}  // namespace

Q4T_TEST(speaker_features_silence_matches_mean_normalization) {
  return Golden("silence", 1600);
}
Q4T_TEST(speaker_features_impulses_match_center_zero_padding) {
  return Golden("impulse", 321);
}
Q4T_TEST(speaker_features_tone_matches_periodic_hamming_and_htk) {
  return Golden("tone", 8000);
}
Q4T_TEST(speaker_features_mixture_matches_official_db_clipping) {
  return Golden("mixture", 16000);
}
Q4T_TEST(speaker_features_noise_matches_full_official_matrix) {
  return Golden("noise", 2563);
}
Q4T_TEST(speaker_features_partial_hop_preserves_final_centered_frame) {
  return Golden("tail", 16179);
}

Q4T_TEST(speaker_features_invalid_input_budget_and_overflow_keep_owner) {
  const SpeakerFeatures original{12, {3.0f, 4.0f}};
  SpeakerFeatures output = original;
  const auto unchanged = [&] {
    return output.frames == original.frames && output.mel == original.mel;
  };
  std::vector<float> samples(160, 0.0f);
  Q4T_CHECK(!ComputeSpeakerFeatures(samples, 8000, {}, &output) && unchanged());
  Q4T_CHECK(!ComputeSpeakerFeatures({}, 16000, {}, &output) && unchanged());
  Q4T_CHECK(!ComputeSpeakerFeatures(samples, 16000, {.max_samples = 159},
                                    &output) && unchanged());
  Q4T_CHECK(!ComputeSpeakerFeatures(samples, 16000, {.max_frames = 1},
                                    &output) && unchanged());
  for (float invalid : {std::numeric_limits<float>::infinity(),
                         std::numeric_limits<float>::quiet_NaN(),
                         std::numeric_limits<float>::max()}) {
    samples[157] = invalid;
    Q4T_CHECK(!ComputeSpeakerFeatures(samples, 16000, {}, &output) && unchanged());
  }
  samples[157] = 0;
  Q4T_CHECK(!ComputeSpeakerFeatures(samples, 16000, {}, nullptr));
  Q4T_CHECK(ComputeSpeakerFeatures(samples, 16000, {}, &output));
  Q4T_CHECK(output.frames == 2 && output.mel.size() == 160);
  Q4T_CHECK(std::all_of(output.mel.begin(), output.mel.end(),
                        [](float x) { return x == 0; }));
  Q4T_CHECK(ComputeSpeakerFeatures(std::span(samples).first(1), 16000, {},
                                   &output));
  Q4T_CHECK(output.frames == 1 && output.mel.size() == 80);
  return true;
}

Q4T_TEST(speaker_features_cancel_at_input_frames_and_publication_keep_owner) {
  const auto samples = Fixture("tail-pcm.f32");
  for (int stop_at : {1, 8, 80}) {
    SpeakerFeatures output{12, {3.0f, 4.0f}};
    int calls = 0;
    Q4T_CHECK(!ComputeSpeakerFeatures(samples, 16000, {}, &output,
                                       [&] { return ++calls >= stop_at; }));
    Q4T_CHECK(output.frames == 12 && output.mel == std::vector<float>({3, 4}));
  }
  SpeakerFeatures complete;
  int count = 0;
  Q4T_CHECK(ComputeSpeakerFeatures(samples, 16000, {}, &complete,
                                    [&] { ++count; return false; }));
  SpeakerFeatures output{12, {3.0f, 4.0f}};
  int calls = 0;
  Q4T_CHECK(!ComputeSpeakerFeatures(samples, 16000, {}, &output,
                                     [&] { return ++calls == count; }));
  Q4T_CHECK(output.frames == 12 && output.mel == std::vector<float>({3, 4}));
  return true;
}
