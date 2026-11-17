#include "q4t/audio/diarization.h"
#include "q4t/test.h"

#include <cmath>
#include <limits>
#include <numbers>

namespace {
using namespace q4t::audio;
SpeakerEmbedding Voice(double angle, float scale = 1) {
  SpeakerEmbedding result{};
  result[0] = scale * std::cos(angle * std::numbers::pi / 180.);
  result[1] = scale * std::sin(angle * std::numbers::pi / 180.);
  return result;
}
std::vector<SpeakerWindow> Windows(size_t count) {
  std::vector<SpeakerWindow> result;
  for (size_t i = 0; i < count; ++i)
    result.push_back({i * 640, (i + 1) * 640, i * 640, (i + 1) * 640});
  return result;
}
}  // namespace

Q4T_TEST(diarization_windows_exact_coverage_and_partial_tail) {
  std::vector<SpeakerWindow> windows;
  const std::vector<VadChunk> chunks{{123, 160127, false}};
  Q4T_CHECK(PlanSpeakerWindows(chunks, 160200, {}, &windows));
  Q4T_CHECK(windows.size() == 11);
  Q4T_CHECK(windows.front().begin_sample == 123);
  Q4T_CHECK(windows.back().end_sample == 160127);
  size_t end = 123;
  for (const auto& window : windows) {
    Q4T_CHECK(window.end_sample - window.begin_sample == 48000);
    Q4T_CHECK(window.core_begin_sample == end);
    Q4T_CHECK(window.core_begin_sample >= window.begin_sample);
    Q4T_CHECK(window.core_end_sample <= window.end_sample);
    Q4T_CHECK(window.core_end_sample > window.core_begin_sample);
    end = window.core_end_sample;
  }
  Q4T_CHECK(end == 160127);
  return true;
}

Q4T_TEST(diarization_windows_join_forced_clips_but_keep_silence) {
  const std::vector<VadChunk> chunks{
      {16, 32016, true}, {32016, 64016, false}, {80000, 80001, false}};
  std::vector<SpeakerWindow> windows;
  Q4T_CHECK(PlanSpeakerWindows(chunks, 96000, {}, &windows));
  Q4T_CHECK(windows.size() == 4);
  Q4T_CHECK(windows[0].end_sample == 48016);
  Q4T_CHECK(windows[2].core_end_sample == 64016);
  Q4T_CHECK(windows[3] == (SpeakerWindow{80000, 80001, 80000, 80001}));
  Q4T_CHECK(PlanSpeakerWindows({}, 96000, {}, &windows) && windows.empty());
  return true;
}

Q4T_TEST(diarization_window_limits_cancel_and_owner_atomicity) {
  const std::vector<SpeakerWindow> original{{1, 2, 1, 2}};
  auto out = original;
  const std::vector<VadChunk> chunks{{0, 160000, false}};
  Q4T_CHECK(!PlanSpeakerWindows(chunks, 160000, {.max_windows = 1}, &out));
  Q4T_CHECK(out == original);
  Q4T_CHECK(!PlanSpeakerWindows(chunks, 159999, {}, &out));
  Q4T_CHECK(out == original);
  Q4T_CHECK(!PlanSpeakerWindows(chunks, 160000, {.step_samples = 0}, &out));
  Q4T_CHECK(out == original);
  const std::vector<VadChunk> invalid{{0, 100, false}, {99, 200, false}};
  Q4T_CHECK(!PlanSpeakerWindows(invalid, 200, {}, &out));
  Q4T_CHECK(out == original);
  for (int stop : {1, 4, 10}) {
    int calls = 0;
    Q4T_CHECK(!PlanSpeakerWindows(chunks, 160000, {}, &out,
                                  [&] { return ++calls == stop; }));
    Q4T_CHECK(out == original);
  }
  return true;
}

Q4T_TEST(diarization_returning_voice_uses_sound_and_stable_labels) {
  const auto windows = Windows(5);
  const std::vector<SpeakerEmbedding> values{Voice(0), Voice(4, 20), Voice(100),
                                             Voice(105, .2), Voice(2)};
  std::vector<SpeakerTurn> turns;
  Q4T_CHECK(ClusterSpeakerWindows(windows, values, {}, {}, &turns));
  Q4T_CHECK(turns ==
            (std::vector<SpeakerTurn>{
                {0, 1280, "A"}, {1280, 2560, "B"}, {2560, 3200, "A"}}));
  auto swapped = values;
  for (auto& voice : swapped) std::swap(voice[0], voice[1]);
  std::vector<SpeakerTurn> again;
  Q4T_CHECK(ClusterSpeakerWindows(windows, swapped, {}, {}, &again));
  Q4T_CHECK(turns == again);
  return true;
}

Q4T_TEST(diarization_average_linkage_rejects_transitive_voice_chain) {
  const auto windows = Windows(3);
  const std::vector<SpeakerEmbedding> values{Voice(0), Voice(40), Voice(80)};
  std::vector<SpeakerTurn> turns;
  Q4T_CHECK(
      ClusterSpeakerWindows(windows, values, {}, {.merge_cosine = .7}, &turns));
  Q4T_CHECK(turns.size() == 2);
  Q4T_CHECK(turns.front().speaker == "A" && turns.back().speaker == "B");
  Q4T_CHECK(turns.front().end_sample == turns.back().begin_sample);
  Q4T_CHECK(turns.back().end_sample == 1920);
  return true;
}

Q4T_TEST(diarization_known_reference_matching_and_name_collision) {
  const auto windows = Windows(3);
  const std::vector<SpeakerEmbedding> values{Voice(0), Voice(100), Voice(2)};
  const std::vector<SpeakerReference> known{{"A", Voice(100)}};
  std::vector<SpeakerTurn> turns;
  Q4T_CHECK(ClusterSpeakerWindows(windows, values, known, {}, &turns));
  Q4T_CHECK(turns[0].speaker == "B" && turns[1].speaker == "A" &&
            turns[2].speaker == "B");
  // Two disjoint acoustic clusters can both match the same explicit reference.
  const std::vector<SpeakerEmbedding> changed{Voice(-35), Voice(35)};
  const std::vector<SpeakerReference> reference{{"caller", Voice(0)}};
  Q4T_CHECK(ClusterSpeakerWindows(Windows(2), changed, reference,
                                  {.max_speakers = 1}, &turns));
  Q4T_CHECK(turns.size() == 1 && turns[0].speaker == "caller");
  return true;
}

Q4T_TEST(diarization_ambiguous_reference_and_identity_capacity_fail) {
  const std::vector<SpeakerTurn> original{{3, 4, "retained"}};
  auto out = original;
  const std::vector<SpeakerEmbedding> values{Voice(0), Voice(100)};
  Q4T_CHECK(!ClusterSpeakerWindows(Windows(2), values, {}, {.max_speakers = 1},
                                   &out));
  Q4T_CHECK(out == original);
  std::vector<SpeakerReference> refs{{"one", Voice(0)}, {"two", Voice(0)}};
  Q4T_CHECK(!ClusterSpeakerWindows(Windows(2), values, refs, {}, &out));
  Q4T_CHECK(out == original);
  refs[1].name = "one";
  Q4T_CHECK(!ClusterSpeakerWindows(Windows(2), values, refs, {}, &out));
  Q4T_CHECK(out == original);
  return true;
}

Q4T_TEST(diarization_invalid_embeddings_and_intervals_preserve_owner) {
  const std::vector<SpeakerTurn> original{{3, 4, "retained"}};
  auto out = original;
  auto windows = Windows(2);
  std::vector<SpeakerEmbedding> values{Voice(0), Voice(100)};
  for (float invalid : {std::numeric_limits<float>::quiet_NaN(),
                        std::numeric_limits<float>::infinity()}) {
    values[1][20] = invalid;
    Q4T_CHECK(!ClusterSpeakerWindows(windows, values, {}, {}, &out));
    Q4T_CHECK(out == original);
  }
  values[1] = {};
  Q4T_CHECK(!ClusterSpeakerWindows(windows, values, {}, {}, &out));
  values[1] = Voice(100);
  windows[1].core_begin_sample = 639;
  Q4T_CHECK(!ClusterSpeakerWindows(windows, values, {}, {}, &out));
  Q4T_CHECK(!ClusterSpeakerWindows(Windows(1), values, {}, {}, &out));
  Q4T_CHECK(!ClusterSpeakerWindows(Windows(2), values, {}, {.merge_cosine = 1.},
                                   &out));
  Q4T_CHECK(out == original);
  Q4T_CHECK(ClusterSpeakerWindows({}, {}, {}, {}, &out) && out.empty());
  return true;
}

Q4T_TEST(diarization_cancel_every_phase_and_final_publication) {
  const auto windows = Windows(32);
  std::vector<SpeakerEmbedding> values;
  for (int i = 0; i < 32; ++i) values.push_back(Voice(i % 2 ? 90 : 0));
  std::vector<SpeakerTurn> complete;
  int total = 0;
  Q4T_CHECK(ClusterSpeakerWindows(windows, values, {}, {}, &complete, [&] {
    ++total;
    return false;
  }));
  for (int stop : {1, 20, 60, total / 2, total - 1, total}) {
    const std::vector<SpeakerTurn> original{{3, 4, "retained"}};
    auto out = original;
    int calls = 0;
    Q4T_CHECK(!ClusterSpeakerWindows(windows, values, {}, {}, &out,
                                     [&] { return ++calls == stop; }));
    Q4T_CHECK(out == original);
  }
  return true;
}
