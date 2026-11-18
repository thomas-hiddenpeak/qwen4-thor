#include "q4t/audio/tts_config.h"
#include "q4t/test.h"

#include <algorithm>
#include <filesystem>
#include <fstream>
#include <limits>
#include <numeric>
#include <sstream>
#include <stdexcept>

namespace {
using namespace q4t::audio;
using q4t::io::Json;
constexpr auto kSmall = TtsVariant::kCustomVoice06B;
constexpr auto kLarge = TtsVariant::kCustomVoice17B;
constexpr uint64_t kSmallBytes = 1811577344;
constexpr uint64_t kLargeBytes = 3833352704;

Json Fixture(const char* name) {
  std::ifstream file(
      std::filesystem::path(__FILE__).parent_path() / "fixtures/tts" / name,
      std::ios::binary);
  std::ostringstream text;
  text << file.rdbuf();
  Json value;
  if (!file || !q4t::io::ParseJson(text.str(), &value, false,
                                   {.reject_duplicate_keys = true}))
    throw std::runtime_error(std::string("TTS fixture: ") + name);
  return value;
}

Json& Field(Json& value, const std::string& name) {
  for (auto& [key, child] : value.object)
    if (key == name) return child;
  throw std::runtime_error("TTS missing fixture field: " + name);
}

Json& Talker(Json& value) { return Field(value, "talker_config"); }
Json& Predictor(Json& value) {
  return Field(Talker(value), "code_predictor_config");
}

uint64_t Bytes(const std::vector<TtsTensorContract>& tensors) {
  return std::accumulate(tensors.begin(), tensors.end(), uint64_t{},
                         [](auto n, const auto& t) { return n + t.bytes; });
}
}  // namespace

Q4T_TEST(tts_fixed_configs_accept_official_profiles_and_reject_cross_binding) {
  auto small = Fixture("config-06b.json"), large = Fixture("config-17b.json");
  Q4T_CHECK(ValidateTtsConfig(small, kSmall));
  Q4T_CHECK(ValidateTtsConfig(large, kLarge));
  Q4T_CHECK(!ValidateTtsConfig(small, kLarge));
  Q4T_CHECK(!ValidateTtsConfig(large, kSmall));
  // JSON member order and exporter provenance do not affect the graph.
  std::reverse(small.object.begin(), small.object.end());
  std::reverse(Talker(small).object.begin(), Talker(small).object.end());
  Field(small, "transformers_version").str = "exporter-provenance-only";
  Q4T_CHECK(ValidateTtsConfig(small, kSmall));
  Q4T_CHECK(ValidateTtsGenerationConfig(Fixture("generation.json")));
  Q4T_CHECK(ValidateTtsCodecConfig(Fixture("codec-config.json")));
  Q4T_CHECK(!ValidateTtsConfig(small, static_cast<TtsVariant>(9)));
  return true;
}

Q4T_TEST(tts_config_rejects_attention_projection_and_execution_overrides) {
  const auto original = Fixture("config-17b.json");
  for (const char* name :
       {"hidden_size", "text_hidden_size", "head_dim", "intermediate_size",
        "num_attention_heads", "num_key_value_heads", "num_code_groups",
        "rms_norm_eps", "rope_theta"}) {
    auto value = original;
    Field(Talker(value), name).number += 0.5;
    Q4T_CHECK(!ValidateTtsConfig(value, kLarge));
  }
  auto value = original;
  Field(Predictor(value), "hidden_size").number = 2048;
  Q4T_CHECK(!ValidateTtsConfig(value, kLarge));
  value = original;
  Field(Predictor(value), "layer_types").array[0].str = "sliding_attention";
  Q4T_CHECK(!ValidateTtsConfig(value, kLarge));
  value = original;
  Field(Talker(value), "attention_bias").boolean = true;
  Q4T_CHECK(!ValidateTtsConfig(value, kLarge));
  value = original;
  Talker(value).object.emplace_back("tie_word_embeddings", Json{});
  Q4T_CHECK(!ValidateTtsConfig(value, kLarge));
  value = original;
  Predictor(value).object.push_back(Predictor(value).object.front());
  Q4T_CHECK(!ValidateTtsConfig(value, kLarge));
  value = original;
  Field(Talker(value), "rms_norm_eps").number =
      std::numeric_limits<double>::quiet_NaN();
  Q4T_CHECK(!ValidateTtsConfig(value, kLarge));
  return true;
}

Q4T_TEST(tts_voice_language_tokens_and_sampling_defaults_are_not_silent) {
  const auto original = Fixture("config-06b.json");
  for (const auto* name : {"tts_bos_token_id", "tts_pad_token_id",
                           "tts_eos_token_id", "im_end_token_id"}) {
    auto value = original;
    Field(value, name).number++;
    Q4T_CHECK(!ValidateTtsConfig(value, kSmall));
  }
  auto value = original;
  Field(Field(Talker(value), "spk_id"), "ryan").number = 3066;
  Q4T_CHECK(!ValidateTtsConfig(value, kSmall));
  value = original;
  Field(Field(Talker(value), "spk_is_dialect"), "eric").str = "english";
  Q4T_CHECK(!ValidateTtsConfig(value, kSmall));
  value = original;
  Field(Field(Talker(value), "codec_language_id"), "chinese").number++;
  Q4T_CHECK(!ValidateTtsConfig(value, kSmall));
  value = original;
  Field(Field(Talker(value), "rope_scaling"), "mrope_section")
      .array[0]
      .number--;
  Q4T_CHECK(!ValidateTtsConfig(value, kSmall));
  for (const auto* name : {"temperature", "subtalker_temperature", "top_k",
                           "repetition_penalty", "max_new_tokens"}) {
    auto generation = Fixture("generation.json");
    Field(generation, name).number++;
    Q4T_CHECK(!ValidateTtsGenerationConfig(generation));
  }
  auto generation = Fixture("generation.json");
  Field(generation, "subtalker_dosample").type = Json::Type::kNumber;
  Q4T_CHECK(!ValidateTtsGenerationConfig(generation));
  return true;
}

Q4T_TEST(tts_talker_graph_matches_both_independent_checkpoint_headers) {
  const auto small = TtsTensorContracts(kSmall);
  const auto large = TtsTensorContracts(kLarge);
  Q4T_CHECK(small.size() == 402 && large.size() == 404);
  Q4T_CHECK(Bytes(small) == kSmallBytes && Bytes(large) == kLargeBytes);
  Q4T_CHECK(ValidateTtsTensorHeader(Fixture("talker-06b-header.json"),
                                    kSmallBytes, kSmall));
  Q4T_CHECK(ValidateTtsTensorHeader(Fixture("talker-17b-header.json"),
                                    kLargeBytes, kLarge));
  Q4T_CHECK(!ValidateTtsTensorHeader(Fixture("talker-06b-header.json"),
                                     kSmallBytes, kLarge));
  const auto header = Fixture("talker-17b-header.json");
  const auto* table = header.Find(
      "talker.code_predictor.model."
      "codec_embedding.0.weight");
  Q4T_CHECK(table && table->GetArray("shape")->array[1].AsInt(-1) == 2048);
  const auto* head = header.Find("talker.code_predictor.lm_head.0.weight");
  Q4T_CHECK(head && head->GetArray("shape")->array[1].AsInt(-1) == 1024);
  Q4T_CHECK(TtsTensorContracts(static_cast<TtsVariant>(9)).empty());
  return true;
}

Q4T_TEST(tts_talker_header_rejects_dtype_missing_heads_and_wrong_projections) {
  const auto original = Fixture("talker-17b-header.json");
  for (const char* name :
       {"talker.model.layers.27.self_attn.q_norm.weight",
        "talker.code_predictor.lm_head.14.weight",
        "talker.code_predictor.small_to_mtp_projection.weight",
        "talker.model.text_embedding.weight"}) {
    auto value = original;
    Field(Field(value, name), "dtype").str = "F16";
    Q4T_CHECK(!ValidateTtsTensorHeader(value, kLargeBytes, kLarge));
    value = original;
    Field(Field(value, name), "shape").array[0].number += 0.5;
    Q4T_CHECK(!ValidateTtsTensorHeader(value, kLargeBytes, kLarge));
    value = original;
    std::erase_if(value.object,
                  [&](const auto& item) { return item.first == name; });
    Q4T_CHECK(!ValidateTtsTensorHeader(value, kLargeBytes, kLarge));
  }
  auto value = original;
  value.object.push_back(value.object.back());
  Q4T_CHECK(!ValidateTtsTensorHeader(value, kLargeBytes, kLarge));
  Q4T_CHECK(!ValidateTtsTensorHeader(original, kLargeBytes + 2, kLarge));
  return true;
}

Q4T_TEST(tts_codec_graph_matches_real_rvq_transformer_and_waveform_weights) {
  const auto graph = TtsCodecDecoderTensorContracts();
  Q4T_CHECK(graph.size() == 271);
  Q4T_CHECK(Bytes(graph) == TtsConfig::kCodecDecoderBytes);
  Q4T_CHECK(ValidateTtsCodecTensorHeader(Fixture("codec-header.json"),
                                         TtsConfig::kCodecFileDataBytes));
  for (const char* name :
       {"sliding_window", "latent_dim", "codebook_dim", "num_quantizers",
        "num_semantic_quantizers", "rms_norm_eps", "rope_theta"}) {
    auto config = Fixture("codec-config.json");
    Field(Field(config, "decoder_config"), name).number++;
    Q4T_CHECK(!ValidateTtsCodecConfig(config));
  }
  auto config = Fixture("codec-config.json");
  Field(config, "decode_upsample_rate").number = 2000;
  Q4T_CHECK(!ValidateTtsCodecConfig(config));
  config = Fixture("codec-config.json");
  Field(Field(config, "decoder_config"), "upsample_rates").array[1].number++;
  Q4T_CHECK(!ValidateTtsCodecConfig(config));
  return true;
}

Q4T_TEST(tts_codec_header_checks_active_graph_and_all_inactive_storage) {
  const auto original = Fixture("codec-header.json");
  for (const char* name :
       {"decoder.quantizer.rvq_first.output_proj.weight",
        "decoder.decoder.1.block.1.conv.weight",
        "decoder.pre_transformer.layers.7.mlp_layer_scale.scale"}) {
    auto value = original;
    Field(Field(value, name), "shape").array[0].number++;
    Q4T_CHECK(
        !ValidateTtsCodecTensorHeader(value, TtsConfig::kCodecFileDataBytes));
  }
  auto value = original;
  auto found = std::find_if(
      value.object.begin(), value.object.end(),
      [](const auto& item) { return item.first.starts_with("encoder."); });
  Q4T_CHECK(found != value.object.end());
  Field(found->second, "dtype").str = "BF16";
  Q4T_CHECK(
      !ValidateTtsCodecTensorHeader(value, TtsConfig::kCodecFileDataBytes));
  value = original;
  Json metadata;
  Q4T_CHECK(q4t::io::ParseJson("{\"format\":\"pt\"}", &metadata));
  metadata.object.push_back(metadata.object.front());
  value.object.emplace_back("__metadata__", std::move(metadata));
  Q4T_CHECK(
      !ValidateTtsCodecTensorHeader(value, TtsConfig::kCodecFileDataBytes));
  value = original;
  value.object.emplace_back("extra.weight", original.object.back().second);
  Q4T_CHECK(
      !ValidateTtsCodecTensorHeader(value, TtsConfig::kCodecFileDataBytes));
  return true;
}

Q4T_TEST(tts_weight_extents_reject_overlap_holes_fractional_and_unbounded) {
  const auto original = Fixture("talker-06b-header.json");
  const std::string key = "talker.model.text_embedding.weight";
  for (double start :
       {-1.0, 0.5, double(kSmallBytes), std::numeric_limits<double>::infinity(),
        std::numeric_limits<double>::quiet_NaN()}) {
    auto value = original;
    Field(Field(value, key), "data_offsets").array[0].number = start;
    Q4T_CHECK(!ValidateTtsTensorHeader(value, kSmallBytes, kSmall));
  }
  auto value = original;
  auto& offsets = Field(Field(value, key), "data_offsets").array;
  offsets[0].number -= 2;
  offsets[1].number -= 2;
  Q4T_CHECK(!ValidateTtsTensorHeader(value, kSmallBytes, kSmall));
  value = original;
  Field(Field(value, key), "shape").array[0].number = 1e100;
  Q4T_CHECK(!ValidateTtsTensorHeader(value, kSmallBytes, kSmall));
  Q4T_CHECK(!ValidateTtsTensorHeader(
      original, std::numeric_limits<uint64_t>::max(), kSmall));
  Q4T_CHECK(!ValidateTtsCodecTensorHeader(Json{}, 0));
  return true;
}
