#include "q4t/audio/diarization.h"
#include "q4t/test.h"

namespace {
using namespace q4t::audio;
SpeakerEmbedding Voice(size_t dimension) {
  SpeakerEmbedding value{};
  value[dimension] = 1;
  return value;
}
const std::vector<SpeakerWindow> kWindows{{0, 4000, 0, 4000},
                                          {0, 4000, 0, 4000},
                                          {2000, 6000, 2000, 6000},
                                          {2000, 6000, 2000, 6000}};
const std::vector<SpeakerEmbedding> kVoices{Voice(0), Voice(1), Voice(1),
                                            Voice(0)};
}  // namespace

Q4T_TEST(overlapping_voices_follow_acoustics_across_channel_swaps) {
  std::vector<SpeakerTurn> turns;
  Q4T_CHECK(LabelOverlappingSpeakerWindows(kWindows, kVoices, {}, {}, &turns));
  Q4T_CHECK(turns.size() == 4);
  Q4T_CHECK(turns[0] == (SpeakerTurn{0, 4000, "A"}));
  Q4T_CHECK(turns[1] == (SpeakerTurn{0, 4000, "B"}));
  Q4T_CHECK(turns[2] == (SpeakerTurn{2000, 6000, "B"}));
  Q4T_CHECK(turns[3] == (SpeakerTurn{2000, 6000, "A"}));
  const auto prior = turns;
  Q4T_CHECK(!ClusterSpeakerWindows(kWindows, kVoices, {}, {}, &turns));
  Q4T_CHECK(turns == prior);
  return true;
}

Q4T_TEST(overlapping_references_and_capacity_remain_request_scoped) {
  std::vector<SpeakerTurn> turns;
  const std::vector<SpeakerReference> refs{{"guest", Voice(1)},
                                           {"owner", Voice(0)}};
  Q4T_CHECK(
      LabelOverlappingSpeakerWindows(kWindows, kVoices, refs, {}, &turns));
  Q4T_CHECK(turns[0].speaker == "owner" && turns[1].speaker == "guest");
  Q4T_CHECK(turns[2].speaker == "guest" && turns[3].speaker == "owner");
  const auto prior = turns;
  Q4T_CHECK(!LabelOverlappingSpeakerWindows(kWindows, kVoices, refs,
                                            {.max_speakers = 1}, &turns));
  Q4T_CHECK(turns == prior);
  auto ambiguous = refs;
  ambiguous[0].embedding = Voice(0);
  Q4T_CHECK(!LabelOverlappingSpeakerWindows(kWindows, kVoices, ambiguous, {},
                                            &turns));
  Q4T_CHECK(turns == prior);
  Q4T_CHECK(LabelOverlappingSpeakerWindows(kWindows, kVoices, {}, {}, &turns));
  Q4T_CHECK(turns[0].speaker == "A");
  return true;
}

Q4T_TEST(overlapping_bad_order_and_cancel_preserve_prior_owner) {
  std::vector<SpeakerTurn> turns{{1, 2, "prior"}};
  const auto prior = turns;
  auto bad = kWindows;
  std::swap(bad.front(), bad.back());
  Q4T_CHECK(!LabelOverlappingSpeakerWindows(bad, kVoices, {}, {}, &turns));
  Q4T_CHECK(turns == prior);
  int total = 0;
  Q4T_CHECK(
      LabelOverlappingSpeakerWindows(kWindows, kVoices, {}, {}, &turns, [&] {
        ++total;
        return false;
      }));
  for (int stop : {1, total / 2, total}) {
    turns = prior;
    int calls = 0;
    Q4T_CHECK(!LabelOverlappingSpeakerWindows(kWindows, kVoices, {}, {}, &turns,
                                              [&] { return ++calls >= stop; }));
    Q4T_CHECK(turns == prior);
  }
  return true;
}
