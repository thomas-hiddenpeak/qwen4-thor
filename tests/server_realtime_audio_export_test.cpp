#include "q4t/server/realtime_audio_export.h"

#include <algorithm>
#include <cmath>
#include <limits>
#include <numbers>
#include <utility>
#include <vector>

#include "q4t/server/realtime_pcm.h"
#include "q4t/test.h"

namespace {
using namespace q4t::server;
#include "server_realtime_audio_export_g711.inc"

std::vector<float> Read(std::string_view bytes, RealtimeAudioEncoding format) {
  const size_t width = format == RealtimeAudioEncoding::kPcm16 ? 2 : 1;
  std::vector<float> values(bytes.size() / width);
  for (size_t i = 0; i < values.size(); ++i)
    values[i] = DecodeRealtimeSample(format, uint8_t(bytes[i * width]),
                                     width == 2 ? uint8_t(bytes[i * 2 + 1])
                                                : 0);
  return values;
}
}  // namespace

Q4T_TEST(realtime_export_g711_all_linear_values_match_independent_oracle) {
  for (bool mu : {false, true}) {
    const std::span<const std::pair<int, uint8_t>> table =
        mu ? std::span<const std::pair<int, uint8_t>>(kMuEncoding)
           : std::span<const std::pair<int, uint8_t>>(kAEncoding);
    size_t index = 0;
    for (int value = -32768; value <= 32767; ++value) {
      if (index + 1 < table.size() && value == table[index + 1].first)
        ++index;
      Q4T_CHECK(EncodeRealtimeG711(int16_t(value), mu) == table[index].second);
    }
  }
  return true;
}

Q4T_TEST(realtime_export_rates_tail_and_dc_match_actual_sample_extent) {
  for (const auto format :
       {RealtimeAudioFormat{},
        RealtimeAudioFormat{RealtimeAudioEncoding::kMuLaw, 8000},
        RealtimeAudioFormat{RealtimeAudioEncoding::kALaw, 8000}}) {
    for (const size_t count : {2, 3, 47, 48, 49, 1023, 1024, 1025, 4111}) {
      const std::vector<float> input(count, .25f);
      std::string bytes;
      Q4T_CHECK(EncodeRealtimeAudio(input, format, 100000, &bytes));
      const auto decoded = Read(bytes, format.encoding);
      Q4T_CHECK(decoded.size() == count * format.rate / 16000);
      for (size_t i = 100; i + 100 < decoded.size(); ++i)
        Q4T_CHECK(std::abs(decoded[i] - .25f) < .009f);
      Q4T_CHECK(input == std::vector<float>(count, .25f));
    }
  }
  return true;
}

Q4T_TEST(realtime_export_resamples_before_saturation_and_preserves_source) {
  // A 6 kHz out-of-band signal exceeds 1. Clipping BEFORE low-pass would
  // introduce a strong 2 kHz alias; the correct 8 kHz export remains silent.
  std::vector<float> input(16000);
  for (size_t i = 0; i < input.size(); ++i)
    input[i] = 2 * std::sin(2 * std::numbers::pi * 6000 * i / 16000 + .3);
  const auto original = input;
  std::string bytes;
  Q4T_CHECK(EncodeRealtimeAudio(
      input, {RealtimeAudioEncoding::kMuLaw, 8000}, 8000, &bytes));
  const auto decoded = Read(bytes, RealtimeAudioEncoding::kMuLaw);
  for (size_t i = 100; i + 100 < decoded.size(); ++i)
    Q4T_CHECK(std::abs(decoded[i]) < .001f);
  Q4T_CHECK(input == original);
  // Finite low-frequency overshoot is saturated only by final S16 conversion.
  input.assign(2048, 2);
  Q4T_CHECK(EncodeRealtimeAudio(input, {}, 6144, &bytes));
  auto pcm = Read(bytes, RealtimeAudioEncoding::kPcm16);
  Q4T_CHECK(pcm[1500] == 32767.f / 32768);
  input.assign(2048, -2);
  Q4T_CHECK(EncodeRealtimeAudio(input, {}, 6144, &bytes));
  pcm = Read(bytes, RealtimeAudioEncoding::kPcm16);
  Q4T_CHECK(pcm[1500] == -1);
  return true;
}

Q4T_TEST(realtime_export_invalid_input_and_exact_budget_are_transactional) {
  std::vector<float> input(101, .1f);
  const auto size = (101 * 24000 / 16000) * 2;
  std::string out = "unchanged";
  Q4T_CHECK(!EncodeRealtimeAudio(input, {}, size - 1, &out));
  Q4T_CHECK(out == "unchanged");
  for (const auto format :
       {RealtimeAudioFormat{RealtimeAudioEncoding::kPcm16, 16000},
        RealtimeAudioFormat{RealtimeAudioEncoding::kMuLaw, 24000},
        RealtimeAudioFormat{RealtimeAudioEncoding::kFloat32, 16000},
        RealtimeAudioFormat{RealtimeAudioEncoding::kALaw, 0}}) {
    Q4T_CHECK(!EncodeRealtimeAudio(input, format, 10000, &out));
    Q4T_CHECK(out == "unchanged");
  }
  for (const float invalid : {std::numeric_limits<float>::infinity(),
                              std::numeric_limits<float>::quiet_NaN()}) {
    input.back() = invalid;
    Q4T_CHECK(!EncodeRealtimeAudio(input, {}, 10000, &out));
    Q4T_CHECK(out == "unchanged");
  }
  Q4T_CHECK(!EncodeRealtimeAudio({}, {}, 10000, &out));
  Q4T_CHECK(!EncodeRealtimeAudio(input, {}, 10000, nullptr));
  input.back() = .1f;
  Q4T_CHECK(EncodeRealtimeAudio(input, {}, size, &out));
  Q4T_CHECK(out.size() == size);
  return true;
}

Q4T_TEST(realtime_export_cancellation_before_during_and_at_publish) {
  const std::vector<float> input(16001, .1f);
  int calls = 0;
  std::string out;
  Q4T_CHECK(EncodeRealtimeAudio(input, {}, 50000, &out, [&] {
    ++calls;
    return false;
  }));
  const auto total = calls;
  Q4T_CHECK(total > 3);
  for (const int cancel_at : {1, total / 2, total}) {
    calls = 0;
    out = "unchanged";
    Q4T_CHECK(!EncodeRealtimeAudio(input, {}, 50000, &out, [&] {
      return ++calls == cancel_at;
    }));
    Q4T_CHECK(out == "unchanged" && calls == cancel_at);
  }
  return true;
}
