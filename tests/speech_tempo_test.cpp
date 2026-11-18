#include "q4t/audio/speech_tempo.h"
#include "q4t/test.h"

#include <algorithm>
#include <cmath>
#include <limits>
#include <numbers>

namespace {
using namespace q4t::audio;
bool Scale(std::span<const float> pcm, double speed, size_t packet,
           std::vector<float>* result, bool* progressive = nullptr) {
  SpeechTempo tempo;
  Q4T_CHECK(tempo.Init(speed, pcm.size(), std::ceil(pcm.size() / speed)));
  size_t fed = 0, calls = 0;
  result->clear();
  while (++calls < 200000) {
    SpeechTempoStep step;
    std::span<const float> output;
    Q4T_CHECK(tempo.Advance(&step, &output));
    Q4T_CHECK(tempo.buffered_samples() <= SpeechTempo::kBufferSamples);
    if (step == SpeechTempoStep::kDone) break;
    if (step == SpeechTempoStep::kNeedInput) {
      Q4T_CHECK(fed < pcm.size());
      const size_t count = std::min(packet, pcm.size() - fed);
      Q4T_CHECK(
          tempo.Append(pcm.subspan(fed, count), fed + count == pcm.size()));
      fed += count;
    } else {
      Q4T_CHECK(!output.empty() && output.size() <= SpeechTempo::kHop);
      if (progressive && fed < pcm.size()) *progressive = true;
      result->insert(result->end(), output.begin(), output.end());
    }
  }
  Q4T_CHECK(calls < 200000 && fed == pcm.size());
  Q4T_CHECK(tempo.input_samples() == pcm.size());
  Q4T_CHECK(tempo.output_samples() == result->size());
  Q4T_CHECK(result->size() == size_t(std::ceil(pcm.size() / speed)));
  return true;
}
std::vector<float> Tone(size_t count, double frequency) {
  std::vector<float> values(count);
  for (size_t i = 0; i < count; ++i)
    values[i] = 0.6 * std::sin(2 * std::numbers::pi * frequency * i / 24000);
  return values;
}
}  // namespace

Q4T_TEST(speech_tempo_identity_is_exact_and_progressive) {
  auto pcm = Tone(24001, 243);
  std::vector<float> output;
  bool progressive = false;
  Q4T_CHECK(Scale(pcm, 1, 1024, &output, &progressive));
  Q4T_CHECK(output == pcm && progressive);
  return true;
}

Q4T_TEST(speech_tempo_partition_does_not_change_audio_or_duration) {
  auto pcm = Tone(12017, 243);
  for (size_t i = 0; i < pcm.size(); ++i)
    pcm[i] *= 0.3 + 0.5 * std::abs(std::sin(double(i) / 701));
  for (const double speed : {0.25, 0.5, 1.3, 2.0, 2.01, 3.1, 4.0}) {
    std::vector<float> a, b;
    Q4T_CHECK(Scale(pcm, speed, 37, &a));
    Q4T_CHECK(Scale(pcm, speed, 4096, &b));
    Q4T_CHECK(a == b);
    Q4T_CHECK(std::all_of(a.begin(), a.end(), [](float x) {
      return std::isfinite(x) && std::abs(x) <= 1;
    }));
  }
  return true;
}

Q4T_TEST(speech_tempo_preserves_tone_pitch_at_speed_extremes) {
  const auto pcm = Tone(24000, 240);
  for (const double speed : {0.25, 0.5, 2.0, 4.0}) {
    std::vector<float> output;
    Q4T_CHECK(Scale(pcm, speed, 1024, &output));
    size_t crossings = 0;
    constexpr size_t margin = 960;
    for (size_t i = margin + 1; i < output.size() - margin; ++i)
      if (output[i - 1] <= 0 && output[i] > 0) ++crossings;
    const double hz = double(crossings) * 24000 / (output.size() - 2 * margin);
    Q4T_CHECK(std::abs(hz - 240) < 10);
  }
  return true;
}

Q4T_TEST(speech_tempo_bounds_fail_atomically_and_short_tail_is_real) {
  SpeechTempo tempo;
  for (const double invalid :
       {0.249, 4.01, std::numeric_limits<double>::quiet_NaN()})
    Q4T_CHECK(!tempo.Init(invalid, 100, 400));
  Q4T_CHECK(tempo.Init(0.5, 100, 200));
  const std::array<float, 1> bad{std::numeric_limits<float>::quiet_NaN()};
  Q4T_CHECK(!tempo.Append(bad, true) && tempo.input_samples() == 0);
  Q4T_CHECK(!tempo.Append({}, true));
  std::vector<float> large(101, 0.2f);
  Q4T_CHECK(!tempo.Append(large, true) && tempo.input_samples() == 0);
  const std::array<float, 1> one{0.25f};
  Q4T_CHECK(tempo.Append(one, false));
  Q4T_CHECK(!tempo.Append(one, true));
  SpeechTempoStep step;
  std::span<const float> bytes;
  Q4T_CHECK(tempo.Advance(&step, &bytes) &&
            step == SpeechTempoStep::kNeedInput);
  Q4T_CHECK(tempo.Append({}, true));
  Q4T_CHECK(tempo.Advance(&step, &bytes) && step == SpeechTempoStep::kOutput);
  Q4T_CHECK(bytes.size() == 2 && bytes[0] == 0.25f && bytes[1] == 0.25f);
  Q4T_CHECK(tempo.Advance(&step, &bytes) && step == SpeechTempoStep::kDone);
  Q4T_CHECK(!tempo.Advance(&step, &bytes));
  for (double speed : {2.01, 3.1, 4.0}) {
    for (size_t length : {1u, 17u, 239u, 481u, 1237u}) {
      std::vector<float> short_pcm(length, 0.25f), output;
      Q4T_CHECK(Scale(short_pcm, speed, 7, &output));
      Q4T_CHECK(std::all_of(output.begin(), output.end(),
                            [](float x) { return x == 0.25f; }));
    }
  }
  SpeechTempo cascade_budget;
  Q4T_CHECK(cascade_budget.Init(3.1, 100, 1));
  Q4T_CHECK(!cascade_budget.Append(std::span(large).first(4), true));
  Q4T_CHECK(cascade_budget.input_samples() == 0);
  SpeechTempo budget;
  Q4T_CHECK(budget.Init(0.25, 100, 3));
  Q4T_CHECK(!budget.Append(one, true) && budget.input_samples() == 0);
  return true;
}
