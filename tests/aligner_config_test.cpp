#include "q4t/audio/aligner_config.h"
#include "q4t/test.h"

#include <algorithm>
#include <filesystem>
#include <fstream>
#include <limits>
#include <sstream>
#include <stdexcept>

namespace {
using namespace q4t::audio;
using q4t::io::Json;
constexpr uint64_t kDataBytes = 1835457792;

Json Fixture(const char* name, const char* family = "aligner") {
  std::ifstream file(std::filesystem::path(__FILE__).parent_path() /
                         "fixtures" / family / name,
                     std::ios::binary);
  std::ostringstream text;
  text << file.rdbuf();
  Json value;
  if (!file || !q4t::io::ParseJson(text.str(), &value, false,
                                   {.reject_duplicate_keys = true}))
    throw std::runtime_error(std::string("aligner fixture: ") + name);
  return value;
}

Json& Member(Json& object, const std::string& name) {
  for (auto& [key, value] : object.object)
    if (key == name) return value;
  throw std::runtime_error("missing fixture member: " + name);
}

Json& Nested(Json& object, const char* name) {
  return Member(Member(object, "thinker_config"), name);
}
}  // namespace

Q4T_TEST(aligner_config_accepts_pinned_metadata_and_rejects_token_drift) {
  const auto original = Fixture("config.json");
  Q4T_CHECK(ValidateAlignerConfig(original));
  for (const char* name : {"timestamp_token_id", "timestamp_segment_time"}) {
    auto changed = original;
    Member(changed, name).number += 0.5;
    Q4T_CHECK(!ValidateAlignerConfig(changed));
  }
  auto changed = original;
  Member(Member(changed, "thinker_config"), "audio_token_id").number++;
  Q4T_CHECK(!ValidateAlignerConfig(changed));
  changed = original;
  Member(changed, "support_languages")
      .array.push_back(Member(changed, "support_languages").array.front());
  Q4T_CHECK(!ValidateAlignerConfig(changed));
  changed = original;
  changed.object.push_back(changed.object.front());
  Q4T_CHECK(!ValidateAlignerConfig(changed));
  Q4T_CHECK(!ValidateAlignerConfig(Json{}));
  return true;
}

Q4T_TEST(aligner_config_rejects_encoder_decoder_and_classifier_drift) {
  const auto original = Fixture("config.json");
  for (const char* name :
       {"d_model", "encoder_layers", "num_hidden_layers",
        "encoder_attention_heads", "encoder_ffn_dim", "num_mel_bins",
        "downsample_hidden_size", "n_window", "n_window_infer", "output_dim"}) {
    auto changed = original;
    Member(Nested(changed, "audio_config"), name).number += 0.5;
    Q4T_CHECK(!ValidateAlignerConfig(changed));
  }
  for (const char* name :
       {"hidden_size", "intermediate_size", "num_hidden_layers",
        "num_attention_heads", "num_key_value_heads", "head_dim", "vocab_size",
        "max_position_embeddings", "rms_norm_eps", "rope_theta"}) {
    auto changed = original;
    Member(Nested(changed, "text_config"), name).number += 0.5;
    Q4T_CHECK(!ValidateAlignerConfig(changed));
  }
  auto changed = original;
  Member(Nested(changed, "text_config"), "tie_word_embeddings").boolean = true;
  Q4T_CHECK(!ValidateAlignerConfig(changed));
  changed = original;
  Member(Member(changed, "thinker_config"), "classify_num").number = 152064;
  Q4T_CHECK(!ValidateAlignerConfig(changed));
  changed = original;
  Member(Member(changed, "thinker_config"), "classify_num").type =
      Json::Type::kBool;
  Q4T_CHECK(!ValidateAlignerConfig(changed));
  changed = original;
  Member(Member(Nested(changed, "text_config"), "rope_scaling"),
         "mrope_section")
      .array[0]
      .number--;
  Q4T_CHECK(!ValidateAlignerConfig(changed));
  return true;
}

Q4T_TEST(aligner_preprocessing_keeps_trained_audio_math) {
  const auto original = Fixture("preprocessor_config.json");
  Q4T_CHECK(ValidateAlignerPreprocessorConfig(original));
  Q4T_CHECK(ValidateAsrPreprocessorConfig(original));
  for (const char* name :
       {"feature_size", "n_fft", "hop_length", "dither", "chunk_length",
        "n_samples", "nb_max_frames", "padding_value"}) {
    auto changed = original;
    Member(changed, name).number++;
    Q4T_CHECK(!ValidateAlignerPreprocessorConfig(changed));
  }
  auto changed = original;
  Json rate;
  rate.type = Json::Type::kNumber;
  rate.number = 48000;
  changed.object.emplace_back("sampling_rate", rate);
  Q4T_CHECK(!ValidateAlignerPreprocessorConfig(changed));
  return true;
}

Q4T_TEST(aligner_generation_metadata_cannot_enable_sampling_or_asr_head) {
  const auto original = Fixture("generation_config.json");
  Q4T_CHECK(ValidateAlignerGenerationConfig(original));
  auto changed = original;
  Member(changed, "do_sample").boolean = true;
  Q4T_CHECK(!ValidateAlignerGenerationConfig(changed));
  changed = original;
  Member(changed, "eos_token_id").array[1].number = 151705;
  Q4T_CHECK(!ValidateAlignerGenerationConfig(changed));
  Q4T_CHECK(!ValidateAlignerGenerationConfig(
      Fixture("generation_config.json", "asr")));
  Q4T_CHECK(!ValidateAsrGenerationConfig(original));
  return true;
}

Q4T_TEST(aligner_tensor_contract_separates_vocabulary_and_timestamp_head) {
  const auto header = Fixture("tensor-header.json");
  Q4T_CHECK(ValidateAlignerTensorHeader(header, kDataBytes));
  Q4T_CHECK(AlignerTensorContracts().size() == 708);
  Q4T_CHECK(AlignerWeightBytes() == kDataBytes);
  const auto* input = header.Find("thinker.model.embed_tokens.weight");
  const auto* output = header.Find("thinker.lm_head.weight");
  Q4T_CHECK(input && output);
  Q4T_CHECK(input->GetArray("shape")->array[0].AsInt(-1) == 152064);
  Q4T_CHECK(output->GetArray("shape")->array[0].AsInt(-1) == 5000);
  auto changed = header;
  Member(changed, "thinker.lm_head.weight") = *input;
  Q4T_CHECK(!ValidateAlignerTensorHeader(changed, kDataBytes));
  return true;
}

Q4T_TEST(aligner_tensor_header_rejects_dtype_range_overlap_and_duplicates) {
  const auto original = Fixture("tensor-header.json");
  const std::string name = "thinker.audio_tower.layers.23.fc1.weight";
  auto changed = original;
  Member(Member(changed, name), "dtype").str = "F16";
  Q4T_CHECK(!ValidateAlignerTensorHeader(changed, kDataBytes));
  changed = original;
  auto& shape = Member(Member(changed, name), "shape").array;
  std::swap(shape[0], shape[1]);
  Q4T_CHECK(!ValidateAlignerTensorHeader(changed, kDataBytes));
  changed = original;
  Member(Member(changed, name), "shape").array[0].number += 0.25;
  Q4T_CHECK(!ValidateAlignerTensorHeader(changed, kDataBytes));
  changed = original;
  auto& offsets = Member(Member(changed, "thinker.audio_tower.conv2d2.bias"),
                         "data_offsets")
                      .array;
  offsets[0].number = 0;
  offsets[1].number = 960;  // Overlaps conv2d1.bias with a valid byte count.
  Q4T_CHECK(!ValidateAlignerTensorHeader(changed, kDataBytes));
  changed = original;
  Member(Member(changed, name), "data_offsets").array[1].number =
      std::numeric_limits<double>::infinity();
  Q4T_CHECK(!ValidateAlignerTensorHeader(changed, kDataBytes));
  changed = original;
  changed.object.push_back(changed.object[1]);
  Q4T_CHECK(!ValidateAlignerTensorHeader(changed, kDataBytes));
  Q4T_CHECK(!ValidateAlignerTensorHeader(original, kDataBytes - 2));
  Q4T_CHECK(!ValidateAlignerTensorHeader(original, kDataBytes + 2));
  return true;
}

Q4T_TEST(aligner_and_asr_contracts_reject_each_others_assets) {
  const auto asr = Fixture("config.json", "asr");
  const auto aligner = Fixture("config.json");
  Q4T_CHECK(ValidateAsrConfig(asr));
  Q4T_CHECK(!ValidateAlignerConfig(asr));
  Q4T_CHECK(!ValidateAsrConfig(aligner));
  const auto asr_header = Fixture("tensor-header.json", "asr");
  Q4T_CHECK(ValidateAsrTensorHeader(asr_header, 1876017152));
  Q4T_CHECK(!ValidateAlignerTensorHeader(asr_header, 1876017152));
  Q4T_CHECK(
      !ValidateAsrTensorHeader(Fixture("tensor-header.json"), kDataBytes));
  auto changed = asr;
  Member(Nested(changed, "audio_config"), "encoder_layers").number = 24;
  Q4T_CHECK(!ValidateAsrConfig(changed));
  changed = asr;
  Member(Member(changed, "thinker_config"), "dtype").str = "float16";
  Q4T_CHECK(!ValidateAsrConfig(changed));
  auto changed_header = asr_header;
  Member(Member(changed_header, "thinker.lm_head.weight"), "data_offsets") =
      Member(Member(changed_header, "thinker.model.embed_tokens.weight"),
             "data_offsets");
  Q4T_CHECK(!ValidateAsrTensorHeader(changed_header, 1876017152));
  return true;
}
