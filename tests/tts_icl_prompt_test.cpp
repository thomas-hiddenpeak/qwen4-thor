#include "q4t/audio/tts_generation.h"
#include "q4t/audio/tts_layout.h"
#include "q4t/test.h"

#include <algorithm>
#include <limits>
#include <utility>

namespace {
using namespace q4t::audio;
constexpr std::array<int32_t, 2> kReference{101, 102};
constexpr std::array<int32_t, 2> kTarget{201, 202};
constexpr size_t kUnlimited = std::numeric_limits<size_t>::max();
std::vector<float> Speaker() { return std::vector<float>(2048, .25f); }
std::vector<TtsCodes> Codes(size_t frames) {
  std::vector<TtsCodes> result(frames);
  for (size_t i = 0; i < frames; ++i)
    for (size_t group = 0; group < 16; ++group)
      result[i][group] = (i * 37 + group * 61) % 2048;
  return result;
}
bool Plan(size_t frames, bool non_streaming, TtsPrompt* out,
          size_t context = 128) {
  return PlanTtsBaseIclPrompt(kTarget, kReference, Codes(frames), {}, Speaker(),
                              "auto", non_streaming, context, out)
      .ok();
}
std::vector<int32_t> Trailing(const TtsPrompt& p) {
  std::vector<int32_t> result;
  for (const auto index : p.trailing_text) result.push_back(p.text_ids[index]);
  return result;
}
}  // namespace

Q4T_TEST(tts_icl_nonstreaming_matches_official_two_track_sequence) {
  TtsPrompt p;
  Q4T_CHECK(Plan(2, true, &p) && ValidateTtsPrompt(p));
  // Official order: role, tags/speaker/BOS, ref text+target+EOS with
  // codec PAD, codec BOS with text PAD, then both complete reference frames.
  const std::vector<int32_t> ids{151644, 77091, 198, 151671, 151672,
                                 151673, 101,   102, 201,    202};
  Q4T_CHECK(p.text_ids == ids);
  const std::vector<std::pair<int, int>> rows{
      {0, -1},   {1, -1},   {2, -1},   {3, 2155}, {3, 2156}, {3, 2157},
      {3, -2},   {4, 2148}, {6, 2148}, {7, 2148}, {8, 2148}, {9, 2148},
      {5, 2148}, {3, 2149}, {3, -3},   {3, -4}};
  Q4T_CHECK(p.prefill.size() == rows.size());
  for (size_t i = 0; i < rows.size(); ++i)
    Q4T_CHECK(p.prefill[i].text_index == rows[i].first &&
              p.prefill[i].codec_id == rows[i].second);
  Q4T_CHECK(Trailing(p) == std::vector<int32_t>{151671});
  return true;
}

Q4T_TEST(tts_icl_streaming_short_reference_retains_remaining_target_and_eos) {
  TtsPrompt p;
  Q4T_CHECK(Plan(1, false, &p) && ValidateTtsPrompt(p));
  Q4T_CHECK(p.prefill.size() == 10);
  Q4T_CHECK(p.text_ids[p.prefill[8].text_index] == 101 &&
            p.prefill[8].codec_id == 2149);
  Q4T_CHECK(p.text_ids[p.prefill[9].text_index] == 102 &&
            p.prefill[9].codec_id == -3);
  Q4T_CHECK(Trailing(p) == std::vector<int32_t>({201, 202, 151673}));
  return true;
}

Q4T_TEST(tts_icl_streaming_long_reference_pads_after_text_eos) {
  TtsPrompt p;
  Q4T_CHECK(Plan(6, false, &p) && ValidateTtsPrompt(p));
  Q4T_CHECK(p.prefill.size() == 15);
  const std::array<int, 6> text{102, 201, 202, 151673, 151671, 151671};
  for (size_t i = 0; i < text.size(); ++i) {
    Q4T_CHECK(p.text_ids[p.prefill[9 + i].text_index] == text[i]);
    Q4T_CHECK(p.prefill[9 + i].codec_id == -3 - static_cast<int32_t>(i));
  }
  Q4T_CHECK(Trailing(p) == std::vector<int32_t>{151671});
  Q4T_CHECK(Plan(4, false, &p) && ValidateTtsPrompt(p));
  Q4T_CHECK(p.text_ids[p.prefill.back().text_index] == 151673);
  Q4T_CHECK(Trailing(p) == std::vector<int32_t>{151671});
  return true;
}

Q4T_TEST(tts_icl_keeps_instruction_language_and_owned_reference) {
  auto ref = Codes(2);
  auto speaker = Speaker();
  auto transcript = kReference;
  TtsPrompt p;
  const std::array<int32_t, 1> instruction{301};
  Q4T_CHECK(PlanTtsBaseIclPrompt(kTarget, transcript, ref, instruction, speaker,
                                 "English", false, 128, &p));
  Q4T_CHECK(ValidateTtsPrompt(p));
  const std::vector<int32_t> prefix{151644, 872,    198,   301, 151645,
                                    198,    151644, 77091, 198};
  Q4T_CHECK(std::equal(prefix.begin(), prefix.end(), p.text_ids.begin()));
  Q4T_CHECK(p.prefill[9].codec_id == 2154 && p.prefill[11].codec_id == 2050);
  Q4T_CHECK(p.reference_codes == ref && p.speaker_embedding == speaker);
  ref[0][15] = 2047;
  speaker[0] = 99;
  transcript[0] = 151644;
  Q4T_CHECK(p.reference_codes[0][15] == 915 && p.speaker_embedding[0] == .25f);
  Q4T_CHECK(std::find(p.text_ids.begin(), p.text_ids.end(), 101) !=
            p.text_ids.end());
  return true;
}

Q4T_TEST(tts_icl_rejects_missing_and_non_audio_reference_atomically) {
  TtsPrompt p;
  Q4T_CHECK(Plan(2, true, &p));
  const auto before = p.prefill.size();
  for (const int bad : {-1, 2048, 2150, std::numeric_limits<int>::max()}) {
    auto ref = Codes(2);
    ref[1][15] = bad;
    Q4T_CHECK(!PlanTtsBaseIclPrompt(kTarget, kReference, ref, {}, Speaker(),
                                    "auto", true, 128, &p));
    Q4T_CHECK(p.prefill.size() == before && p.reference_codes == Codes(2));
  }
  Q4T_CHECK(!PlanTtsBaseIclPrompt(kTarget, {}, Codes(2), {}, Speaker(), "auto",
                                  true, 128, &p));
  Q4T_CHECK(!PlanTtsBaseIclPrompt(kTarget, kReference, {}, {}, Speaker(),
                                  "auto", true, 128, &p));
  Q4T_CHECK(!Plan(2, true, nullptr));
  return true;
}

Q4T_TEST(tts_icl_rejects_control_tokens_and_context_truncation) {
  TtsPrompt p;
  Q4T_CHECK(Plan(2, true, &p, 16));
  Q4T_CHECK(!Plan(2, true, &p, 15));
  Q4T_CHECK(p.prefill.size() == 16 && p.reference_codes.size() == 2);
  const std::array<int32_t, 1> control{151644};
  Q4T_CHECK(!PlanTtsBaseIclPrompt(kTarget, control, Codes(2), {}, Speaker(),
                                  "auto", true, 128, &p));
  Q4T_CHECK(!PlanTtsBaseIclPrompt(control, kReference, Codes(2), {}, Speaker(),
                                  "auto", true, 128, &p));
  Q4T_CHECK(!PlanTtsBaseIclPrompt(kTarget, kReference, Codes(2), control,
                                  Speaker(), "auto", true, 128, &p));
  Q4T_CHECK(!Plan(2, true, &p, 32769));
  return true;
}

Q4T_TEST(tts_icl_reference_frames_must_be_complete_ordered_and_base_only) {
  TtsPrompt good;
  Q4T_CHECK(Plan(2, true, &good));
  for (int mutation = 0; mutation < 10; ++mutation) {
    auto p = good;
    if (mutation == 0) p.reference_codes.clear();
    if (mutation == 1) p.reference_codes.push_back(Codes(1)[0]);
    if (mutation == 2) p.prefill.pop_back();
    if (mutation == 3) std::swap(p.prefill[14], p.prefill[15]);
    if (mutation == 4) p.prefill[15].codec_id = -3;
    if (mutation == 5) p.prefill[13].codec_id = 2148;
    if (mutation == 6) p.speaker_embedding.clear();
    if (mutation == 7) p.prefill[15].codec_id = std::numeric_limits<int>::min();
    if (mutation == 8) p.reference_codes[1][15] = 2048;
    if (mutation == 9) p.prefill.push_back({p.pad_index, 2148});
    Q4T_CHECK(!ValidateTtsPrompt(p));
  }
  return true;
}

Q4T_TEST(tts_icl_full_reference_limit_is_bounded_without_truncation) {
  TtsPrompt p;
  Q4T_CHECK(Plan(750, false, &p, 759) && ValidateTtsPrompt(p));
  Q4T_CHECK(p.reference_codes.size() == 750 && p.prefill.size() == 759);
  Q4T_CHECK(p.prefill.back().codec_id == -752);
  Q4T_CHECK(!Plan(751, false, &p, 1024));
  Q4T_CHECK(p.reference_codes.size() == 750 && p.prefill.size() == 759);
  return true;
}

Q4T_TEST(tts_icl_workspace_charges_all_groups_and_embeddings_only_when_used) {
  TtsGenerationLayout base, explicit_zero, icl;
  constexpr auto variant = TtsVariant::kBase17B;
  Q4T_CHECK(
      PlanTtsGenerationWorkspace(variant, 16, 16, 160, kUnlimited, &base));
  Q4T_CHECK(PlanTtsGenerationWorkspace(variant, 16, 16, 160, kUnlimited,
                                       &explicit_zero, 0));
  Q4T_CHECK(base.bytes == explicit_zero.bytes && base.reference_codes == 0 &&
            base.reference_embeddings == 0);
  Q4T_CHECK(
      PlanTtsGenerationWorkspace(variant, 16, 16, 160, kUnlimited, &icl, 2));
  Q4T_CHECK(icl.reference_codes % 256 == 0 &&
            icl.reference_embeddings % 256 == 0);
  Q4T_CHECK(icl.reference_embeddings >=
            icl.reference_codes + 2 * sizeof(TtsCodes));
  Q4T_CHECK(icl.prefill == icl.reference_embeddings + 2 * 2048 * sizeof(float));
  Q4T_CHECK(icl.bytes >=
            base.bytes + 2 * (sizeof(TtsCodes) + 2048 * sizeof(float)));
  Q4T_CHECK(
      PlanTtsGenerationWorkspace(variant, 16, 16, 160, icl.bytes, &icl, 2));
  const auto bytes = icl.bytes;
  Q4T_CHECK(
      !PlanTtsGenerationWorkspace(variant, 16, 16, 160, bytes - 1, &icl, 2));
  Q4T_CHECK(icl.bytes == bytes);
  Q4T_CHECK(!PlanTtsGenerationWorkspace(TtsVariant::kCustomVoice17B, 16, 16,
                                        160, kUnlimited, &icl, 2));
  Q4T_CHECK(
      !PlanTtsGenerationWorkspace(variant, 16, 16, 160, kUnlimited, &icl, 16));
  Q4T_CHECK(!PlanTtsGenerationWorkspace(variant, 1024, 1024, 160, kUnlimited,
                                        &icl, 751));
  return true;
}
