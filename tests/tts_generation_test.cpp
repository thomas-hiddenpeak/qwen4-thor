#include "q4t/audio/tts_generation.h"
#include "q4t/audio/tts_layout.h"
#include "q4t/test.h"

#include <algorithm>
#include <bit>
#include <filesystem>
#include <fstream>
#include <limits>
#include <numeric>
#include <sstream>
#include <stdexcept>
#include <tuple>

namespace {
using namespace q4t::audio;
constexpr auto kLarge = TtsVariant::kCustomVoice17B;
constexpr auto kSmall = TtsVariant::kCustomVoice06B;
constexpr size_t kUnlimited = std::numeric_limits<size_t>::max();

q4t::io::Json Header(const char* name) {
  std::ifstream file(std::filesystem::path(__FILE__).parent_path() /
                      "fixtures/tts" / name);
  std::ostringstream bytes;
  bytes << file.rdbuf();
  q4t::io::Json value;
  if (!file || !q4t::io::ParseJson(bytes.str(), &value, false,
                                   {.reject_duplicate_keys = true}))
    throw std::runtime_error("TTS header fixture unavailable");
  return value;
}

uint16_t Bf16(float x) { return std::bit_cast<uint32_t>(x) >> 16; }
}  // namespace

Q4T_TEST(tts_prompt_keeps_instruction_text_and_dual_tracks) {
  TtsPrompt p;
  const std::array<int32_t, 3> text{9707, 11, 1879};
  const std::array<int32_t, 2> instruction{123, 456};
  Q4T_CHECK(PlanTtsPrompt(kLarge, text, instruction, "Ryan", "Auto",
                          false, 128, &p));
  Q4T_CHECK(ValidateTtsPrompt(p));
  Q4T_CHECK(p.prefill.size() == 16 && p.text_ids.size() == 16);
  const std::vector<int32_t> prefix{151644, 872, 198, 123, 456, 151645, 198,
                                   151644, 77091, 198};
  Q4T_CHECK(std::equal(prefix.begin(), prefix.end(), p.text_ids.begin()));
  for (size_t i = 0; i < prefix.size(); ++i)
    Q4T_CHECK(p.prefill[i].text_index == static_cast<int32_t>(i) &&
               p.prefill[i].codec_id == -1);
  const std::array<int, 6> codec{2155, 2156, 2157, 3061, 2148, 2149};
  for (size_t i = 0; i < codec.size(); ++i)
    Q4T_CHECK(p.prefill[prefix.size() + i].codec_id == codec[i]);
  Q4T_CHECK(p.text_ids[p.prefill.back().text_index] == text.front());
  std::vector<int32_t> trailing;
  for (const auto index : p.trailing_text) trailing.push_back(p.text_ids[index]);
  Q4T_CHECK(trailing == std::vector<int32_t>({11, 1879, 151673}));
  return true;
}

Q4T_TEST(tts_prompt_language_dialect_and_nonstreaming_are_explicit) {
  const std::array<int32_t, 2> text{123, 456};
  for (const auto& [voice, language, id] :
       {std::tuple{"eric", "auto", 2062}, {"dylan", "Chinese", 2074},
        {"eric", "English", 2050}, {"serena", "Japanese", 2058}}) {
    TtsPrompt p;
    Q4T_CHECK(PlanTtsPrompt(kLarge, text, {}, voice, language, true, 64, &p));
    Q4T_CHECK(ValidateTtsPrompt(p));
    Q4T_CHECK(p.prefill[3].codec_id == 2154 && p.prefill[5].codec_id == id);
    Q4T_CHECK(p.prefill.back().codec_id == 2149);
    Q4T_CHECK(p.prefill.back().text_index == p.pad_index);
    Q4T_CHECK(p.trailing_text == std::vector<int32_t>{p.pad_index});
    const size_t start = p.prefill.size() - 4;
    Q4T_CHECK(p.text_ids[p.prefill[start].text_index] == 123);
    Q4T_CHECK(p.text_ids[p.prefill[start + 1].text_index] == 456);
    Q4T_CHECK(p.text_ids[p.prefill[start + 2].text_index] == 151673);
    for (size_t i = start; i < start + 3; ++i)
      Q4T_CHECK(p.prefill[i].codec_id == 2148);
  }
  return true;
}

Q4T_TEST(tts_prompt_rejects_wrong_template_ids_and_context_exhaustion) {
  TtsPrompt p;
  const std::array<int32_t, 1> text{9707};
  Q4T_CHECK(PlanTtsPrompt(kLarge, text, {}, "ryan", "auto", false, 9, &p));
  const auto prior = p.text_ids;
  Q4T_CHECK(!PlanTtsPrompt(kLarge, text, {}, "ryan", "auto", false, 8, &p));
  Q4T_CHECK(p.text_ids == prior);
  Q4T_CHECK(!PlanTtsPrompt(kSmall, text, text, "ryan", "auto", false, 64, &p));
  Q4T_CHECK(!PlanTtsPrompt(kLarge, {}, {}, "ryan", "auto", false, 64, &p));
  Q4T_CHECK(!PlanTtsPrompt(kLarge, text, {}, "unknown", "auto", false, 64, &p));
  Q4T_CHECK(!PlanTtsPrompt(kLarge, text, {}, "ryan", "unknown", false, 64, &p));
  for (const int32_t id : {-1, 151644, 151675, 151936}) {
    Q4T_CHECK(!PlanTtsPrompt(kLarge, {&id, 1}, {}, "ryan", "auto", false,
                              64, &p));
    Q4T_CHECK(!PlanTtsPrompt(kLarge, text, {&id, 1}, "ryan", "auto", false,
                              64, &p));
  }
  p.prefill[0].text_index = static_cast<int32_t>(p.text_ids.size());
  Q4T_CHECK(!ValidateTtsPrompt(p));
  return true;
}

Q4T_TEST(tts_weight_layout_binds_only_verified_extents) {
  TtsWeightLayout layout;
  for (const auto variant : {kSmall, kLarge}) {
    const auto header = Header(variant == kSmall ? "talker-06b-header.json"
                                                 : "talker-17b-header.json");
    const size_t bytes = variant == kSmall ? 1811577344 : 3833352704;
    Q4T_CHECK(ParseTtsWeightLayout(header, bytes, variant, &layout));
    Q4T_CHECK(ValidateTtsWeightLayout(layout, variant));
    Q4T_CHECK(layout.bytes == bytes && layout.file_bytes == bytes);
    auto corrupt = layout;
    corrupt.tensors[1].offset = corrupt.tensors[0].offset;
    Q4T_CHECK(!ValidateTtsWeightLayout(corrupt, variant));
    corrupt = layout;
    corrupt.tensors[1].file_offset = corrupt.tensors[0].file_offset;
    Q4T_CHECK(!ValidateTtsWeightLayout(corrupt, variant));
    Q4T_CHECK(!ParseTtsWeightLayout(header, bytes - 1, variant, &layout));
    Q4T_CHECK(layout.bytes == bytes);
  }
  Q4T_CHECK(ParseTtsCodecWeightLayout(Header("codec-header.json"),
                                      TtsConfig::kCodecFileDataBytes, &layout));
  Q4T_CHECK(layout.bytes == TtsConfig::kCodecDecoderBytes);
  Q4T_CHECK(layout.tensors.size() == 271);
  Q4T_CHECK(ValidateTtsWeightLayout(layout, kLarge, true));
  Q4T_CHECK(std::all_of(layout.tensors.begin(), layout.tensors.end(),
                         [](const auto& t) { return t.name.starts_with("decoder."); }));
  layout.tensors.back().file_offset = layout.file_bytes;
  Q4T_CHECK(!ValidateTtsWeightLayout(layout, kLarge, true));
  return true;
}

Q4T_TEST(tts_dense_workspace_covers_cache_temporaries_and_budget) {
  TtsDenseLayout w;
  Q4T_CHECK(PlanTtsDenseWorkspace(kLarge, false, 17, 65, kUnlimited, &w));
  Q4T_CHECK(w.hidden == 2048 && w.layers == 28 && w.intermediate == 6144);
  Q4T_CHECK(w.padded_rows == 32 && w.padded_context == 80);
  const size_t cache_bytes = 28 * 80 * 1024 * sizeof(uint16_t);
  Q4T_CHECK(w.cache_v - w.cache_k == cache_bytes);
  Q4T_CHECK(w.gemm - w.cache_v >= cache_bytes);
  Q4T_CHECK(w.bytes == w.gemm + TtsDenseLayout::kGemmBytes);
  const size_t accepted = w.bytes;
  Q4T_CHECK(PlanTtsDenseWorkspace(kLarge, false, 17, 65, accepted, &w));
  Q4T_CHECK(!PlanTtsDenseWorkspace(kLarge, false, 17, 65, accepted - 1, &w));
  Q4T_CHECK(w.bytes == accepted);
  Q4T_CHECK(!PlanTtsDenseWorkspace(kLarge, false, 2, kUnlimited, kUnlimited, &w));
  Q4T_CHECK(!PlanTtsDenseWorkspace(kLarge, true, 2, 17, kUnlimited, &w));
  Q4T_CHECK(!PlanTtsDenseWorkspace(kLarge, true, 3, 2, kUnlimited, &w));
  Q4T_CHECK(PlanTtsDenseWorkspace(kLarge, true, 2, 16, kUnlimited, &w));
  Q4T_CHECK(w.hidden == 1024 && w.layers == 5 && w.intermediate == 3072);
  return true;
}

Q4T_TEST(tts_generation_workspace_accounts_for_both_models_and_history) {
  TtsGenerationLayout w;
  Q4T_CHECK(PlanTtsGenerationWorkspace(kLarge, 100, 9, 120, kUnlimited, &w));
  Q4T_CHECK(w.talker.context == 129 && w.predictor.context == 16);
  Q4T_CHECK(w.predictor_offset >= w.talker.bytes);
  Q4T_CHECK(w.ids >= w.predictor_offset + w.predictor.bytes);
  Q4T_CHECK(w.affine_fp32 + 100 * 2048 * 4 <= w.prefill);
  Q4T_CHECK(w.text_projected + 100 * 2048 * 2 <= w.affine_fp32);
  Q4T_CHECK(w.logits + 3072 * 2 <= w.bytes);
  const size_t bytes = w.bytes;
  Q4T_CHECK(!PlanTtsGenerationWorkspace(kLarge, 100, 9, 120, bytes - 1, &w));
  Q4T_CHECK(w.bytes == bytes);
  Q4T_CHECK(!PlanTtsGenerationWorkspace(kLarge, kUnlimited, 9, 120,
                                        kUnlimited, &w));
  Q4T_CHECK(!PlanTtsGenerationWorkspace(kLarge, 100, 32768, 2, kUnlimited, &w));
  Q4T_CHECK(!PlanTtsGenerationWorkspace(kLarge, 100, 9, 4097, kUnlimited, &w));
  return true;
}

Q4T_TEST(tts_code_sampling_respects_suppression_penalty_and_top_k) {
  std::vector<uint16_t> logits(3072, Bf16(-100));
  logits[2] = Bf16(2); logits[3] = Bf16(1.5f); logits[3000] = Bf16(100);
  TtsCodeSampler sampler;
  Q4T_CHECK(sampler.Init({.sample = false, .repetition_penalty = 2}, 0));
  int32_t token = -1, repeated = 2;
  Q4T_CHECK(sampler.Select(logits, true, false, {}, &token) && token == 2);
  Q4T_CHECK(sampler.Select(logits, true, false, {&repeated, 1}, &token));
  Q4T_CHECK(token == 3);
  logits[2] = Bf16(-1); logits[3] = Bf16(-1.5f);
  Q4T_CHECK(sampler.Select(logits, true, false, {&repeated, 1}, &token));
  Q4T_CHECK(token == 3);
  logits[2150] = Bf16(90);
  Q4T_CHECK(sampler.Select(logits, true, false, {}, &token) && token == 2);
  Q4T_CHECK(sampler.Select(logits, true, true, {}, &token) && token == 2150);
  Q4T_CHECK(sampler.Init({.top_k = 1}, 77));
  Q4T_CHECK(sampler.Select(logits, true, true, {}, &token) && token == 2150);
  return true;
}

Q4T_TEST(tts_code_sampling_seed_top_p_and_invalid_values) {
  std::vector<uint16_t> logits(2048, Bf16(-50));
  logits[10] = logits[11] = logits[12] = Bf16(0);
  TtsCodeSampler a, b;
  Q4T_CHECK(a.Init({.top_k = 1}, 42) && b.Init({.top_k = 1}, 42));
  std::array<bool, 3> seen{};
  for (int i = 0; i < 64; ++i) {
    int32_t x, y;
    Q4T_CHECK(a.Select(logits, false, false, {}, &x));
    Q4T_CHECK(b.Select(logits, false, false, {}, &y));
    Q4T_CHECK(x == y && x >= 10 && x <= 12);
    seen[x - 10] = true;  // Top-k retains tied scores, not arbitrary IDs.
  }
  Q4T_CHECK(seen[0] && seen[1] && seen[2]);
  Q4T_CHECK(a.Init({.top_k = 0, .top_p = 0.1f}, 42));
  int32_t x;
  Q4T_CHECK(a.Select(logits, false, false, {}, &x) && x == 10);
  logits[99] = Bf16(std::numeric_limits<float>::quiet_NaN());
  Q4T_CHECK(!a.Select(logits, false, false, {}, &x));
  Q4T_CHECK(!a.Init({.temperature = 0}, 1));
  Q4T_CHECK(!a.Init({.top_k = -1}, 1));
  Q4T_CHECK(!a.Init({.top_p = 0}, 1));
  Q4T_CHECK(!a.Init({.repetition_penalty = std::numeric_limits<float>::infinity()},
                     1));
  return true;
}

Q4T_TEST(tts_generation_state_requires_complete_frames_before_progress) {
  TtsGenerationState state;
  Q4T_CHECK(!state.Accept(0, 1));
  Q4T_CHECK(state.Init(10) && !state.Init(10));
  Q4T_CHECK(!state.Accept(0, 2150) && !state.Accept(1, 0));
  for (size_t group = 0; group < 16; ++group) {
    Q4T_CHECK(state.frames() == 0 && state.first_code_history().empty());
    Q4T_CHECK(state.Accept(group, static_cast<int32_t>(group + 2)));
  }
  Q4T_CHECK(state.frames() == 1 && state.next_group() == 0);
  Q4T_CHECK(state.completed()[0][15] == 17);
  Q4T_CHECK(state.first_code_history()[0] == 2);
  Q4T_CHECK(!state.Accept(0, 2048) && !state.Accept(0, -1));
  Q4T_CHECK(state.frames() == 1 && state.next_group() == 0);
  return true;
}

Q4T_TEST(tts_generation_state_eos_and_budget_do_not_return_partial_frames) {
  for (const size_t maximum : {2, 3}) {
    TtsGenerationState state;
    Q4T_CHECK(!state.Init(4097) && state.Init(maximum));
    for (int frame = 0; frame < 2; ++frame)
      for (size_t group = 0; group < 16; ++group)
        Q4T_CHECK(state.Accept(group, 0));
    Q4T_CHECK(state.allow_eos() && !state.ended());
    if (maximum == 2) {
      Q4T_CHECK(state.at_limit() && !state.Accept(0, 0));
      Q4T_CHECK(!state.Accept(0, 2150));
    } else {
      Q4T_CHECK(!state.at_limit() && state.Accept(0, 2150));
      Q4T_CHECK(state.ended() && !state.Accept(0, 0));
    }
    Q4T_CHECK(state.completed().size() == 2);
  }
  return true;
}
