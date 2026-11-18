#include "q4t/audio/tts_speaker_features.h"
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
using q4t::audio::ComputeTtsSpeakerFeatures;
using q4t::audio::TtsSpeakerFeatureFixedBytes;
using q4t::audio::TtsSpeakerFeatureLimits;
using q4t::audio::TtsSpeakerFeatures;

TtsSpeakerFeatures Sentinel() { return {7, {3.0f, 4.0f}}; }
bool Unchanged(const TtsSpeakerFeatures& value) {
  return value.frames == 7 && value.mel == std::vector<float>({3, 4});
}
std::vector<float> Fixture(const std::string& name) {
  const auto path = std::filesystem::path(__FILE__).parent_path() /
                    "fixtures/tts/speaker_features" / name;
  std::ifstream file(path, std::ios::binary | std::ios::ate);
  const auto length = file.tellg();
  if (!file || length <= 0 || length > 1024 * 1024 || length % 4)
    throw std::runtime_error("TTS speaker feature fixture size/file");
  std::vector<unsigned char> bytes(static_cast<size_t>(length));
  file.seekg(0);
  file.read(reinterpret_cast<char*>(bytes.data()), bytes.size());
  if (!file) throw std::runtime_error("TTS speaker feature fixture truncated");
  std::vector<float> values(bytes.size() / 4);
  for (size_t i = 0; i < values.size(); ++i) {
    uint32_t bits = 0;
    for (size_t byte = 0; byte < 4; ++byte)
      bits |= static_cast<uint32_t>(bytes[4 * i + byte]) << (8 * byte);
    values[i] = std::bit_cast<float>(bits);
  }
  return values;
}
int Bf16Ordered(float value) {
  uint32_t bits = std::bit_cast<uint32_t>(value);
  bits += 0x7fff + ((bits >> 16) & 1);
  const int bf16 = bits >> 16;
  return (bf16 & 0x8000) ? 0x8000 - (bf16 & 0x7fff) : 0x8000 + bf16;
}
bool Golden(const std::string& name, size_t count) {
  const auto samples = Fixture(name + "-pcm.f32");
  const auto expected = Fixture(name + "-canonical.f32");
  const auto fp32 = Fixture(name + "-mel.f32");
  Q4T_CHECK(samples.size() == count);
  TtsSpeakerFeatures result;
  Q4T_CHECK(ComputeTtsSpeakerFeatures(samples, 24000, {}, &result));
  Q4T_CHECK(result.frames == count / 256);
  Q4T_CHECK(result.mel.size() == expected.size());
  Q4T_CHECK(result.mel.size() == fp32.size());
  double maximum = 0, fp32_maximum = 0;
  int maximum_bf16_ulp = 0;
  for (size_t i = 0; i < expected.size(); ++i) {
    Q4T_CHECK(std::isfinite(result.mel[i]));
    maximum = std::max(
        maximum, std::abs(static_cast<double>(result.mel[i]) - expected[i]));
    fp32_maximum = std::max(
        fp32_maximum, std::abs(static_cast<double>(result.mel[i]) - fp32[i]));
    maximum_bf16_ulp =
        std::max(maximum_bf16_ulp,
                 std::abs(Bf16Ordered(result.mel[i]) - Bf16Ordered(fp32[i])));
  }
  std::printf(
      "  %s: %zu frames, canonical error %.9g, FP32 difference %.9g, "
      "BF16 ULP %d\n",
      name.c_str(), result.frames, maximum, fp32_maximum, maximum_bf16_ulp);
  Q4T_CHECK(maximum <= 1e-4);
  Q4T_CHECK(maximum_bf16_ulp <= 1);
  return true;
}
}  // namespace

Q4T_TEST(tts_speaker_features_reference_silence) {
  return Golden("silence", 512);
}
Q4T_TEST(tts_speaker_features_reference_reflection_boundary) {
  return Golden("impulse", 385);
}
Q4T_TEST(tts_speaker_features_reference_tone) { return Golden("tone", 2400); }
Q4T_TEST(tts_speaker_features_reference_mixture) {
  return Golden("mixture", 5001);
}
Q4T_TEST(tts_speaker_features_reference_noise) { return Golden("noise", 1997); }
Q4T_TEST(tts_speaker_features_reference_chirp_partial_hop) {
  return Golden("tail", 24017);
}
Q4T_TEST(tts_speaker_features_require_owner_and_exact_rate) {
  std::vector<float> samples(512, 0);
  auto out = Sentinel();
  Q4T_CHECK(!ComputeTtsSpeakerFeatures(samples, 24000, {}, nullptr));
  for (int rate : {0, -24000, 16000, 48000}) {
    Q4T_CHECK(!ComputeTtsSpeakerFeatures(samples, rate, {}, &out));
    Q4T_CHECK(Unchanged(out));
  }
  return true;
}
Q4T_TEST(tts_speaker_features_enforce_reflection_and_duration_bounds) {
  std::vector<float> samples(1440001, 0);
  auto out = Sentinel();
  for (size_t size : {size_t{0}, size_t{1}, size_t{384}, samples.size()}) {
    Q4T_CHECK(!ComputeTtsSpeakerFeatures(std::span(samples).first(size), 24000,
                                         {}, &out));
    Q4T_CHECK(Unchanged(out));
  }
  Q4T_CHECK(ComputeTtsSpeakerFeatures(std::span(samples).first(385), 24000, {},
                                      &out));
  Q4T_CHECK(out.frames == 1 && out.mel.size() == 128);
  return true;
}
Q4T_TEST(tts_speaker_features_enforce_sample_budget_before_work) {
  std::vector<float> samples(512, 0);
  auto out = Sentinel();
  Q4T_CHECK(
      !ComputeTtsSpeakerFeatures(samples, 24000, {.max_samples = 511}, &out));
  Q4T_CHECK(Unchanged(out));
  Q4T_CHECK(
      ComputeTtsSpeakerFeatures(samples, 24000, {.max_samples = 512}, &out));
  return true;
}
Q4T_TEST(tts_speaker_features_enforce_frame_budget_before_work) {
  std::vector<float> samples(512, 0);
  auto out = Sentinel();
  Q4T_CHECK(
      !ComputeTtsSpeakerFeatures(samples, 24000, {.max_frames = 1}, &out));
  Q4T_CHECK(Unchanged(out));
  Q4T_CHECK(ComputeTtsSpeakerFeatures(samples, 24000, {.max_frames = 2}, &out));
  return true;
}
Q4T_TEST(tts_speaker_features_charge_input_output_tables_and_scratch) {
  std::vector<float> samples(512, 0);
  const size_t bytes = TtsSpeakerFeatureFixedBytes() + (512 + 2 * 128) * 4;
  Q4T_CHECK(TtsSpeakerFeatureFixedBytes() > 8 * 1024 * 1024);
  auto out = Sentinel();
  Q4T_CHECK(!ComputeTtsSpeakerFeatures(samples, 24000, {.max_bytes = bytes - 1},
                                       &out));
  Q4T_CHECK(Unchanged(out));
  Q4T_CHECK(
      ComputeTtsSpeakerFeatures(samples, 24000, {.max_bytes = bytes}, &out));
  return true;
}
Q4T_TEST(tts_speaker_features_reject_nonfinite_and_unnormalized_pcm) {
  std::vector<float> samples(512, 0);
  auto out = Sentinel();
  for (float invalid :
       {std::numeric_limits<float>::quiet_NaN(),
        std::numeric_limits<float>::infinity(),
        -std::numeric_limits<float>::infinity(), 1.0001f, -1.0001f}) {
    samples[511] = invalid;
    Q4T_CHECK(!ComputeTtsSpeakerFeatures(samples, 24000, {}, &out));
    Q4T_CHECK(Unchanged(out));
  }
  samples[0] = -1;
  samples[511] = 1;
  Q4T_CHECK(ComputeTtsSpeakerFeatures(samples, 24000, {}, &out));
  return true;
}
Q4T_TEST(tts_speaker_features_cancellation_is_atomic_through_publication) {
  std::vector<float> samples(5001, 0.25f);
  TtsSpeakerFeatures result;
  int calls = 0;
  Q4T_CHECK(ComputeTtsSpeakerFeatures(samples, 24000, {}, &result, [&] {
    ++calls;
    return false;
  }));
  Q4T_CHECK(calls > 10);
  for (int stop_at : {1, 2, 3, 5, calls}) {
    auto out = Sentinel();
    int position = 0;
    Q4T_CHECK(!ComputeTtsSpeakerFeatures(
        samples, 24000, {}, &out, [&] { return ++position == stop_at; }));
    Q4T_CHECK(position == stop_at && Unchanged(out));
  }
  return true;
}
Q4T_TEST(tts_speaker_features_silence_retains_log_floor_and_partial_hop) {
  std::vector<float> samples(767, 0);
  TtsSpeakerFeatures result;
  Q4T_CHECK(ComputeTtsSpeakerFeatures(samples, 24000, {}, &result));
  Q4T_CHECK(result.frames == 2 && result.mel.size() == 256);
  Q4T_CHECK(std::all_of(result.mel.begin(), result.mel.end(),
                        [](float x) { return x == std::log(1e-5f); }));
  return true;
}
