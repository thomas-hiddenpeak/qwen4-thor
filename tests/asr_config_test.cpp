#include "q4t/audio/asr_config.h"
#include "q4t/test.h"

#include <algorithm>
#include <filesystem>
#include <fstream>
#include <limits>
#include <sstream>
#include <stdexcept>

namespace {
using q4t::audio::AsrConfig;
using q4t::audio::ValidateAsrConfig;
using q4t::audio::ValidateAsrTensorHeader;
using q4t::io::Json;
constexpr uint64_t kDataBytes = 1876017152;

Json Fixture(const char* name) {
  std::ifstream file(std::filesystem::path(__FILE__).parent_path() /
                     "fixtures/asr" / name, std::ios::binary);
  std::ostringstream text;
  text << file.rdbuf();
  Json value;
  if (!file || !q4t::io::ParseJson(text.str(), &value, false,
                                  {.reject_duplicate_keys = true}))
    throw std::runtime_error(std::string("ASR fixture: ") + name);
  return value;
}

Json& Member(Json& parent, const std::string& name) {
  for (auto& [key, value] : parent.object)
    if (key == name) return value;
  throw std::runtime_error("ASR fixture member: " + name);
}

Json& Encoder(Json& config) {
  return Member(Member(config, "thinker_config"), "audio_config");
}
Json& Decoder(Json& config) {
  return Member(Member(config, "thinker_config"), "text_config");
}
}  // namespace

Q4T_TEST(asr_config_pins_encoder_decoder_and_independent_output_head) {
  const auto config = Fixture("config.json");
  Q4T_CHECK(ValidateAsrConfig(config));
  const auto tensors = q4t::audio::AsrTensorContracts();
  Q4T_CHECK(tensors.size() == 612);
  Q4T_CHECK(q4t::audio::AsrWeightBytes() == kDataBytes);
  const auto header = Fixture("tensor-header.json");
  Q4T_CHECK(ValidateAsrTensorHeader(header, kDataBytes));
  const auto* input = header.Find("thinker.model.embed_tokens.weight");
  const auto* output = header.Find("thinker.lm_head.weight");
  Q4T_CHECK(input && output);
  Q4T_CHECK(input->GetArray("data_offsets")->array[0].AsInt(-1) !=
            output->GetArray("data_offsets")->array[0].AsInt(-1));
  Q4T_CHECK(AsrConfig::kEncoderHeads * AsrConfig::kEncoderHeadDim ==
            AsrConfig::kEncoderHidden);
  Q4T_CHECK(AsrConfig::kQueryHeads * AsrConfig::kHeadDim != AsrConfig::kHidden);
  return true;
}

Q4T_TEST(asr_config_rejects_compute_parameter_drift_and_wrong_types) {
  const auto original = Fixture("config.json");
  for (const char* name : {"d_model", "encoder_layers", "num_hidden_layers",
                           "encoder_attention_heads", "encoder_ffn_dim",
                           "num_mel_bins", "downsample_hidden_size",
                           "output_dim", "n_window", "n_window_infer",
                           "conv_chunksize", "max_source_positions",
                           "dropout", "attention_dropout",
                           "activation_dropout"}) {
    auto changed = original;
    Member(Encoder(changed), name).number += 0.5;
    Q4T_CHECK(!ValidateAsrConfig(changed));
  }
  for (const char* name : {"hidden_size", "intermediate_size",
                           "num_hidden_layers", "num_attention_heads",
                           "num_key_value_heads", "head_dim", "vocab_size",
                           "max_position_embeddings", "rms_norm_eps",
                           "rope_theta", "attention_dropout"}) {
    auto changed = original;
    Member(Decoder(changed), name).number += 0.5;
    Q4T_CHECK(!ValidateAsrConfig(changed));
  }
  for (const char* name : {"attention_bias", "use_cache",
                           "add_cross_attention", "tie_word_embeddings"}) {
    auto changed = original;
    auto& value = Member(Decoder(changed), name);
    value.boolean = !value.boolean;
    Q4T_CHECK(!ValidateAsrConfig(changed));
  }
  auto changed = original;
  Member(Encoder(changed), "d_model").number =
      std::numeric_limits<double>::quiet_NaN();
  Q4T_CHECK(!ValidateAsrConfig(changed));
  changed = original;
  Member(Decoder(changed), "hidden_size").type = Json::Type::kBool;
  Q4T_CHECK(!ValidateAsrConfig(changed));
  return true;
}

Q4T_TEST(asr_config_rejects_tokens_rope_language_and_duplicate_drift) {
  const auto original = Fixture("config.json");
  for (const char* name : {"audio_start_token_id", "audio_end_token_id",
                           "audio_token_id"}) {
    auto changed = original;
    Member(Member(changed, "thinker_config"), name).number += 1;
    Q4T_CHECK(!ValidateAsrConfig(changed));
  }
  auto changed = original;
  auto& thinker = Member(changed, "thinker_config");
  thinker.object.emplace_back("tie_word_embeddings",
                               Member(Decoder(changed), "tie_word_embeddings"));
  Q4T_CHECK(!ValidateAsrConfig(changed));
  changed = original;
  Member(Member(Decoder(changed), "rope_scaling"), "mrope_section")
      .array[0].number = 23;
  Q4T_CHECK(!ValidateAsrConfig(changed));
  changed = original;
  Member(Member(Decoder(changed), "rope_scaling"), "mrope_interleaved")
      .boolean = false;
  Q4T_CHECK(!ValidateAsrConfig(changed));
  changed = original;
  auto& languages = Member(changed, "support_languages").array;
  languages[1] = languages[0];
  Q4T_CHECK(!ValidateAsrConfig(changed));
  changed = original;
  Encoder(changed).object.push_back(Encoder(changed).object.front());
  Q4T_CHECK(!ValidateAsrConfig(changed));
  changed = original;
  Member(changed, "architectures").array[0].str = "Qwen3ForCausalLM";
  Q4T_CHECK(!ValidateAsrConfig(changed));
  changed = original;
  Member(Member(changed, "thinker_config"), "dtype").str = "float16";
  Q4T_CHECK(!ValidateAsrConfig(changed));
  Q4T_CHECK(!ValidateAsrConfig(Json{}));
  return true;
}

Q4T_TEST(asr_preprocessing_and_generation_reject_changed_math_and_stops) {
  using q4t::audio::ValidateAsrGenerationConfig;
  using q4t::audio::ValidateAsrPreprocessorConfig;
  const auto original = Fixture("preprocessor_config.json");
  Q4T_CHECK(ValidateAsrPreprocessorConfig(original));
  for (const char* name : {"feature_size", "n_fft", "hop_length", "dither",
                           "chunk_length", "n_samples", "nb_max_frames",
                           "padding_value"}) {
    auto changed = original;
    Member(changed, name).number += 1;
    Q4T_CHECK(!ValidateAsrPreprocessorConfig(changed));
  }
  auto changed = original;
  Member(changed, "padding_side").str = "left";
  Q4T_CHECK(!ValidateAsrPreprocessorConfig(changed));
  changed = original;
  Json rate;
  rate.type = Json::Type::kNumber;
  rate.number = 16000;
  changed.object.emplace_back("sampling_rate", rate);
  Q4T_CHECK(ValidateAsrPreprocessorConfig(changed));
  Member(changed, "sampling_rate").number = 48000;
  Q4T_CHECK(!ValidateAsrPreprocessorConfig(changed));
  const auto generation = Fixture("generation_config.json");
  Q4T_CHECK(ValidateAsrGenerationConfig(generation));
  changed = generation;
  Member(changed, "eos_token_id").array[1].number = 151670;
  Q4T_CHECK(!ValidateAsrGenerationConfig(changed));
  changed = generation;
  Member(changed, "do_sample").boolean = true;
  Q4T_CHECK(!ValidateAsrGenerationConfig(changed));
  changed = generation;
  changed.object.emplace_back("forced_eos_token_id", rate);
  Q4T_CHECK(!ValidateAsrGenerationConfig(changed));
  return true;
}

Q4T_TEST(asr_encoder_length_obeys_independent_convolution_chunks) {
  using q4t::audio::AsrEncodedFrames;
  for (size_t frames = 0; frames <= 8192; ++frames) {
    size_t expected = 0;
    for (size_t first = 0; first < frames; first += 100) {
      size_t dimension = std::min(size_t{100}, frames - first);
      for (int layer = 0; layer < 3; ++layer)
        dimension = (dimension + 2 - 3) / 2 + 1;  // k=3,p=1,s=2.
      expected += dimension;
    }
    Q4T_CHECK(AsrEncodedFrames(frames) == expected);
  }
  Q4T_CHECK(AsrEncodedFrames(0) == 0);
  Q4T_CHECK(AsrEncodedFrames(99) == 13);
  Q4T_CHECK(AsrEncodedFrames(100) == 13);
  Q4T_CHECK(AsrEncodedFrames(101) == 14);
  Q4T_CHECK(AsrEncodedFrames(200) == 26);
  Q4T_CHECK(AsrEncodedFrames(3000) == 390);
  const size_t maximum = std::numeric_limits<size_t>::max();
  Q4T_CHECK(AsrEncodedFrames(maximum) >= AsrEncodedFrames(maximum - 100));
  Q4T_CHECK(AsrEncodedFrames(maximum) < maximum / 4);
  return true;
}

Q4T_TEST(asr_tensor_header_rejects_names_dtype_shape_and_duplicate_drift) {
  const auto original = Fixture("tensor-header.json");
  for (const char* name : {"thinker.audio_tower.conv2d1.weight",
                           "thinker.audio_tower.layers.17.fc1.weight",
                           "thinker.model.layers.27.self_attn.q_proj.weight",
                           "thinker.lm_head.weight"}) {
    auto changed = original;
    Member(Member(changed, name), "dtype").str = "F16";
    Q4T_CHECK(!ValidateAsrTensorHeader(changed, kDataBytes));
    changed = original;
    auto& dimensions = Member(Member(changed, name), "shape").array;
    std::swap(dimensions[0], dimensions[1]);  // Same byte count, wrong axes.
    Q4T_CHECK(!ValidateAsrTensorHeader(changed, kDataBytes));
    changed = original;
    Member(Member(changed, name), "shape").array[0].number += 0.25;
    Q4T_CHECK(!ValidateAsrTensorHeader(changed, kDataBytes));
  }
  auto changed = original;
  changed.object.erase(changed.object.begin() + 1);
  Q4T_CHECK(!ValidateAsrTensorHeader(changed, kDataBytes));
  changed = original;
  changed.object.push_back(changed.object[1]);
  Q4T_CHECK(!ValidateAsrTensorHeader(changed, kDataBytes));
  changed = original;
  changed.object[1].first = "wrong.tensor.name";
  Q4T_CHECK(!ValidateAsrTensorHeader(changed, kDataBytes));
  changed = original;
  Member(changed, "__metadata__").type = Json::Type::kNull;
  Q4T_CHECK(!ValidateAsrTensorHeader(changed, kDataBytes));
  return true;
}

Q4T_TEST(asr_tensor_header_rejects_overlap_truncation_holes_and_trailing_data) {
  const auto original = Fixture("tensor-header.json");
  auto changed = original;
  auto& offsets = Member(Member(changed, "thinker.audio_tower.conv2d2.bias"),
                         "data_offsets").array;
  offsets[0].number = 0;
  offsets[1].number = 960;  // Same size as conv2d1.bias but overlaps it.
  Q4T_CHECK(!ValidateAsrTensorHeader(changed, kDataBytes));
  for (const double start : {-1.0, 1.25, 1e30}) {
    changed = original;
    Member(Member(changed, "thinker.audio_tower.conv2d1.bias"), "data_offsets")
        .array[0].number = start;
    Q4T_CHECK(!ValidateAsrTensorHeader(changed, kDataBytes));
  }
  Q4T_CHECK(!ValidateAsrTensorHeader(original, kDataBytes - 1));
  Q4T_CHECK(!ValidateAsrTensorHeader(original, kDataBytes + 1));
  changed = original;
  std::reverse(changed.object.begin(), changed.object.end());
  Q4T_CHECK(ValidateAsrTensorHeader(changed, kDataBytes));
  return true;
}
