#include "q4t/audio/tts_generation.h"
#include "q4t/audio/tts_layout.h"
#include "q4t/audio/tts_speaker_layout.h"
#include "q4t/test.h"

#include <algorithm>
#include <filesystem>
#include <fstream>
#include <limits>
#include <sstream>
#include <stdexcept>

namespace {
using namespace q4t::audio;
constexpr auto kBase = TtsVariant::kBase17B;
constexpr auto kCustom = TtsVariant::kCustomVoice17B;
constexpr size_t kUnlimited = std::numeric_limits<size_t>::max();
constexpr std::array<int32_t, 3> kText{9707, 11, 1879};

q4t::io::Json Fixture(const char* name) {
  std::ifstream file(std::filesystem::path(__FILE__).parent_path() /
                     "fixtures/tts" / name);
  std::ostringstream text;
  text << file.rdbuf();
  q4t::io::Json result;
  if (!file || !q4t::io::ParseJson(text.str(), &result, false,
                                   {.reject_duplicate_keys = true}))
    throw std::runtime_error("TTS independent fixture unavailable");
  return result;
}
std::vector<float> Speaker() {
  std::vector<float> result(2048);
  for (size_t i = 0; i < result.size(); ++i)
    result[i] = (int(i % 17) - 8) / 16.f;
  return result;
}
TtsWeightLayout Layout() {
  TtsWeightLayout result;
  if (!ParseTtsWeightLayout(Fixture("base-17b-header.json"),
                            TtsSpeakerConfig::kBaseDataBytes, kBase, &result))
    throw std::runtime_error("Base weights rejected");
  return result;
}
}  // namespace

Q4T_TEST(tts_base_variant_requires_actual_base_config_and_header) {
  Q4T_CHECK(ValidateTtsConfig(Fixture("base-17b-config.json"), kBase));
  Q4T_CHECK(!ValidateTtsConfig(Fixture("config-17b.json"), kBase));
  Q4T_CHECK(!ValidateTtsConfig(Fixture("base-17b-config.json"), kCustom));
  Q4T_CHECK(ValidateTtsTensorHeader(Fixture("base-17b-header.json"),
                                    TtsSpeakerConfig::kBaseDataBytes, kBase));
  Q4T_CHECK(!ValidateTtsTensorHeader(Fixture("talker-17b-header.json"),
                                     3833352704, kBase));
  return true;
}

Q4T_TEST(tts_base_layout_selects_talker_and_keeps_speaker_prefix_distinct) {
  const auto layout = Layout();
  Q4T_CHECK(layout.tensors.size() == 404 && layout.bytes == 3833352704);
  Q4T_CHECK(layout.file_bytes == TtsSpeakerConfig::kBaseDataBytes);
  Q4T_CHECK(ValidateTtsWeightLayout(layout, kBase));
  Q4T_CHECK(!ValidateTtsWeightLayout(layout, kCustom));
  for (const auto& tensor : layout.tensors) {
    Q4T_CHECK(tensor.name.starts_with("talker."));
    Q4T_CHECK(tensor.file_offset >= TtsSpeakerConfig::kSourceBytes);
  }
  TtsDeviceWeights weights;
  constexpr uintptr_t base = 0x10000000;
  Q4T_CHECK(BindTtsDeviceWeights(reinterpret_cast<const void*>(base),
                                 layout.bytes * 2, layout, kBase, &weights));
  Q4T_CHECK(weights.variant == kBase && weights.predictor_projection.weight);
  const auto found = std::find_if(
      layout.tensors.begin(), layout.tensors.end(), [](const auto& t) {
        return t.name == "talker.code_predictor.small_to_mtp_projection.weight";
      });
  Q4T_CHECK(found != layout.tensors.end());
  Q4T_CHECK(reinterpret_cast<uintptr_t>(weights.predictor_projection.weight) ==
            base + found->offset * 2);
  return true;
}

Q4T_TEST(tts_base_layout_rejects_prefix_overlap_gaps_and_wrong_storage) {
  const auto valid = Layout();
  for (int mutation = 0; mutation < 5; ++mutation) {
    auto value = valid;
    if (mutation == 0) value.tensors.front().file_offset = 0;
    if (mutation == 1) value.tensors.front().file_offset += 2;
    if (mutation == 2) value.file_bytes = value.bytes;
    if (mutation == 3) value.bytes += 2;
    if (mutation == 4) value.tensors.back().file_offset = kUnlimited;
    Q4T_CHECK(!ValidateTtsWeightLayout(value, kBase));
  }
  return true;
}

Q4T_TEST(tts_base_workspace_charges_condition_without_changing_builtin_layout) {
  TtsGenerationLayout base, custom;
  Q4T_CHECK(PlanTtsGenerationWorkspace(kBase, 16, 16, 160, kUnlimited, &base));
  Q4T_CHECK(
      PlanTtsGenerationWorkspace(kCustom, 16, 16, 160, kUnlimited, &custom));
  Q4T_CHECK(base.talker.hidden == 2048 && base.predictor.hidden == 1024);
  Q4T_CHECK(base.bytes == custom.bytes + 2048 * sizeof(float));
  Q4T_CHECK(custom.speaker_condition == 0);
  Q4T_CHECK(base.speaker_condition % 256 == 0);
  Q4T_CHECK(base.prefill == base.speaker_condition + 2048 * sizeof(float));
  Q4T_CHECK(PlanTtsGenerationWorkspace(kBase, 16, 16, 160, base.bytes, &base));
  Q4T_CHECK(
      !PlanTtsGenerationWorkspace(kBase, 16, 16, 160, base.bytes - 1, &base));
  return true;
}

Q4T_TEST(tts_base_prompt_places_owned_vector_at_official_speaker_position) {
  auto source = Speaker();
  TtsPrompt prompt;
  Q4T_CHECK(PlanTtsBasePrompt(kText, {}, source, "auto", false, 128, &prompt));
  Q4T_CHECK(ValidateTtsPrompt(prompt));
  const std::array<int32_t, 9> ids{-1,   -1, -1,   2155, 2156,
                                   2157, -2, 2148, 2149};
  Q4T_CHECK(prompt.prefill.size() == ids.size());
  for (size_t i = 0; i < ids.size(); ++i)
    Q4T_CHECK(prompt.prefill[i].codec_id == ids[i]);
  Q4T_CHECK(prompt.prefill[6].text_index == prompt.pad_index);
  Q4T_CHECK(prompt.text_ids[prompt.prefill[7].text_index] == 151672);
  Q4T_CHECK(prompt.speaker_embedding == source);
  source[0] = 999;
  Q4T_CHECK(prompt.speaker_embedding[0] == -0.5f);
  std::vector<int32_t> trailing;
  for (auto at : prompt.trailing_text) trailing.push_back(prompt.text_ids[at]);
  Q4T_CHECK(trailing == std::vector<int32_t>({11, 1879, 151673}));
  return true;
}

Q4T_TEST(tts_base_nonstreaming_keeps_instruction_language_and_text_tracks) {
  TtsPrompt p;
  const std::array<int32_t, 2> instruction{123, 456};
  Q4T_CHECK(PlanTtsBasePrompt(kText, instruction, Speaker(), "Chinese", true,
                              128, &p));
  Q4T_CHECK(ValidateTtsPrompt(p));
  const std::vector<int32_t> prefix{151644, 872, 198,    123,   456,
                                    151645, 198, 151644, 77091, 198};
  Q4T_CHECK(std::equal(prefix.begin(), prefix.end(), p.text_ids.begin()));
  const size_t start = prefix.size();
  const std::array<int32_t, 6> codes{2154, 2156, 2055, 2157, -2, 2148};
  for (size_t i = 0; i < codes.size(); ++i)
    Q4T_CHECK(p.prefill[start + i].codec_id == codes[i]);
  Q4T_CHECK(p.trailing_text == std::vector<int32_t>{p.pad_index});
  Q4T_CHECK(p.prefill.back().codec_id == 2149 &&
            p.prefill.back().text_index == p.pad_index);
  for (size_t i = 0; i < kText.size(); ++i) {
    const auto row = p.prefill[start + codes.size() + i];
    Q4T_CHECK(p.text_ids[row.text_index] == kText[i] && row.codec_id == 2148);
  }
  return true;
}

Q4T_TEST(tts_base_invalid_condition_or_content_is_atomic) {
  const auto speaker = Speaker();
  TtsPrompt out;
  Q4T_CHECK(PlanTtsBasePrompt(kText, {}, speaker, "auto", false, 128, &out));
  const auto before = out.text_ids;
  for (int mutation = 0; mutation < 5; ++mutation) {
    auto values = speaker;
    if (mutation == 0) values.clear();
    if (mutation == 1) values.pop_back();
    if (mutation == 2) std::fill(values.begin(), values.end(), 0);
    if (mutation == 3) values.back() = std::numeric_limits<float>::quiet_NaN();
    if (mutation == 4) values.front() = std::numeric_limits<float>::infinity();
    Q4T_CHECK(!PlanTtsBasePrompt(kText, {}, values, "auto", false, 128, &out));
    Q4T_CHECK(out.speaker_embedding == speaker && out.text_ids == before);
  }
  Q4T_CHECK(
      !PlanTtsBasePrompt(kText, {}, speaker, "unknown", false, 128, &out));
  Q4T_CHECK(!PlanTtsBasePrompt(kText, {}, speaker, "auto", false, 8, &out));
  const int32_t token = 151644;
  Q4T_CHECK(
      !PlanTtsBasePrompt({&token, 1}, {}, speaker, "auto", false, 128, &out));
  Q4T_CHECK(
      !PlanTtsBasePrompt(kText, {}, speaker, "auto", false, 128, nullptr));
  return true;
}

Q4T_TEST(tts_base_prompt_rejects_missing_duplicate_and_misplaced_condition) {
  TtsPrompt valid;
  Q4T_CHECK(
      PlanTtsBasePrompt(kText, {}, Speaker(), "auto", false, 128, &valid));
  for (int mutation = 0; mutation < 7; ++mutation) {
    auto p = valid;
    if (mutation == 0) p.speaker_embedding.clear();
    if (mutation == 1) p.prefill[6].codec_id = 3061;
    if (mutation == 2) p.prefill[5].codec_id = -2;
    if (mutation == 3) p.prefill[6].text_index = 0;
    if (mutation == 4) p.prefill[6].codec_id = -3;
    if (mutation == 5) std::swap(p.prefill[6], p.prefill[7]);
    if (mutation == 6) p.prefill[7].text_index = 99999;
    Q4T_CHECK(!ValidateTtsPrompt(p));
  }
  return true;
}

Q4T_TEST(tts_base_and_builtin_prompt_entrypoints_cannot_substitute_voices) {
  TtsPrompt p;
  Q4T_CHECK(!PlanTtsPrompt(kBase, kText, {}, "ryan", "auto", false, 128, &p));
  Q4T_CHECK(PlanTtsPrompt(kCustom, kText, {}, "ryan", "auto", false, 128, &p));
  Q4T_CHECK(p.speaker_embedding.empty() && p.prefill[6].codec_id == 3061);
  p.speaker_embedding = Speaker();
  Q4T_CHECK(!ValidateTtsPrompt(p));
  return true;
}
