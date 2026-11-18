#include "q4t/audio/separator_tracks.h"
#include "q4t/test.h"

#include <algorithm>
#include <cmath>
#include <limits>
#include <numbers>
#include <vector>

namespace {
using namespace q4t::audio;
std::vector<float> Tone(size_t count, double hz) {
  std::vector<float> samples(count);
  for (size_t i = 0; i < count; ++i)
    samples[i] = float(std::sin(2 * std::numbers::pi * hz * i / 8000));
  return samples;
}
}  // namespace

Q4T_TEST(separator_tracks_duplicate_gain_polarity_and_dc) {
  const auto source = Tone(32000, 440);
  for (float gain : {.01f, -2.f, 8.f}) {
    auto second = source;
    for (auto& sample : second) sample = sample * gain + .1f;
    SeparationTrackDecision result;
    Q4T_CHECK(InspectSeparationTracks(source, second, &result));
    Q4T_CHECK(result.use_original && result.coherence > .999);
    Q4T_CHECK(!result.independent_region);
  }
  return true;
}

Q4T_TEST(separator_tracks_independent_quiet_source_is_retained) {
  const auto first = Tone(32000, 440);
  for (float gain : {1.f, .01f, .00001f}) {
    auto second = Tone(32000, 688);
    for (auto& sample : second) sample *= gain;
    SeparationTrackDecision result;
    Q4T_CHECK(InspectSeparationTracks(first, second, &result));
    Q4T_CHECK(!result.use_original && result.coherence < .01);
  }
  return true;
}

Q4T_TEST(separator_tracks_local_duplicate_survives_phase_gain_switches) {
  const auto first = Tone(32000, 440);
  auto second = first;
  for (size_t i = 0; i < second.size(); ++i)
    second[i] *= i < 16000 ? .25f : -.25f;
  SeparationTrackDecision result;
  Q4T_CHECK(InspectSeparationTracks(first, second, &result));
  Q4T_CHECK(result.use_original);
  return true;
}

Q4T_TEST(separator_tracks_brief_independent_region_vetoes_collapse) {
  const auto first = Tone(240000, 440);
  auto second = first;
  const auto independent = Tone(first.size(), 688);
  std::copy(independent.begin() + 120000, independent.begin() + 124000,
            second.begin() + 120000);
  SeparationTrackDecision result;
  Q4T_CHECK(InspectSeparationTracks(first, second, &result));
  Q4T_CHECK(result.coherence > .9 && result.coherent_energy_fraction > .9);
  Q4T_CHECK(result.independent_region && !result.use_original);
  return true;
}

Q4T_TEST(separator_tracks_zero_constant_short_and_odd_input) {
  for (size_t count : {size_t{1}, size_t{17}, size_t{240000}}) {
    const auto first = Tone(count, 440);
    std::vector<float> silent(count), constant(count, .5f);
    SeparationTrackDecision result;
    Q4T_CHECK(InspectSeparationTracks(silent, silent, &result));
    Q4T_CHECK(result.use_original);
    Q4T_CHECK(InspectSeparationTracks(first, constant, &result));
    Q4T_CHECK(result.use_original);
  }
  return true;
}

Q4T_TEST(separator_tracks_bad_input_keeps_previous_decision) {
  auto first = Tone(32000, 440);
  SeparationTrackDecision result{true, .9, .8, true};
  const auto unchanged = [&] {
    return result.use_original && result.coherence == .9 &&
           result.coherent_energy_fraction == .8 && result.independent_region;
  };
  Q4T_CHECK(!InspectSeparationTracks({}, {}, &result) && unchanged());
  Q4T_CHECK(!InspectSeparationTracks(first, {}, &result) && unchanged());
  Q4T_CHECK(!InspectSeparationTracks(first, first, nullptr));
  std::vector<float> too_large(240001);
  Q4T_CHECK(!InspectSeparationTracks(too_large, too_large, &result) &&
            unchanged());
  for (float bad : {std::numeric_limits<float>::quiet_NaN(),
                    std::numeric_limits<float>::infinity()}) {
    first.back() = bad;
    Q4T_CHECK(!InspectSeparationTracks(first, first, &result) && unchanged());
  }
  return true;
}

Q4T_TEST(separator_tracks_cancellation_is_atomic) {
  const auto first = Tone(16000, 440);
  for (int stop : {1, 4, 17}) {
    int calls = 0;
    SeparationTrackDecision result{false, .123, .456, true};
    Q4T_CHECK(!InspectSeparationTracks(first, first, &result,
                                       [&] { return ++calls == stop; }));
    Q4T_CHECK(!result.use_original && result.coherence == .123 &&
              result.coherent_energy_fraction == .456 &&
              result.independent_region);
  }
  return true;
}

Q4T_TEST(separator_tracks_symmetric_and_scale_independent) {
  auto a = Tone(32000, 440), b = Tone(32000, 688);
  for (size_t i = 0; i < a.size(); ++i) b[i] = -.7f * a[i] + .3f * b[i];
  SeparationTrackDecision original, swapped, quiet;
  Q4T_CHECK(InspectSeparationTracks(a, b, &original));
  Q4T_CHECK(InspectSeparationTracks(b, a, &swapped));
  for (auto& sample : a) sample *= .0001f;
  for (auto& sample : b) sample *= .0001f;
  Q4T_CHECK(InspectSeparationTracks(a, b, &quiet));
  Q4T_CHECK(original.use_original && swapped.use_original &&
            quiet.use_original);
  Q4T_CHECK(std::abs(original.coherence - swapped.coherence) < 1e-12);
  Q4T_CHECK(std::abs(original.coherence - quiet.coherence) < 1e-6);
  return true;
}
