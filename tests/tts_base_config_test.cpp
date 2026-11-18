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
constexpr uint64_t kBytes = 3857354880;
constexpr auto kSpeaker = "speaker_encoder.fc.weight";

Json Fixture(const char* name) {
  std::ifstream file(std::filesystem::path(__FILE__).parent_path() /
                     "fixtures/tts" / name);
  std::ostringstream text;
  text << file.rdbuf();
  Json value;
  if (!file || !q4t::io::ParseJson(text.str(), &value, false,
                                   {.reject_duplicate_keys = true}))
    throw std::runtime_error("Base TTS fixture unavailable");
  return value;
}
Json& Field(Json& value, const std::string& name) {
  for (auto& [key, child] : value.object)
    if (key == name) return child;
  throw std::runtime_error("Base TTS fixture field missing");
}
Json Config() { return Fixture("base-17b-config.json"); }
Json Header() { return Fixture("base-17b-header.json"); }
}  // namespace

Q4T_TEST(tts_base_config_accepts_fixed_official_metadata_and_key_order) {
  auto config = Config();
  Q4T_CHECK(ValidateTtsBaseConfig(config));
  std::reverse(config.object.begin(), config.object.end());
  Q4T_CHECK(ValidateTtsBaseConfig(config));
  return true;
}

Q4T_TEST(tts_base_config_rejects_cross_model_and_missing_condition) {
  Q4T_CHECK(!ValidateTtsBaseConfig(Fixture("config-17b.json")));
  Q4T_CHECK(!ValidateTtsConfig(Config(), TtsVariant::kCustomVoice17B));
  auto config = Config();
  Field(config, "tts_model_type").str = "custom_voice";
  Q4T_CHECK(!ValidateTtsBaseConfig(config));
  config = Config();
  std::erase_if(config.object, [](const auto& p) {
    return p.first == "speaker_encoder_config";
  });
  Q4T_CHECK(!ValidateTtsBaseConfig(config));
  return true;
}

Q4T_TEST(tts_base_config_rejects_wrong_speaker_rate_and_dimension) {
  for (const auto* name : {"sample_rate", "enc_dim"}) {
    auto config = Config();
    Field(Field(config, "speaker_encoder_config"), name).number = 192;
    Q4T_CHECK(!ValidateTtsBaseConfig(config));
  }
  return true;
}

Q4T_TEST(tts_base_config_rejects_compute_overrides_and_duplicate_fields) {
  auto config = Config();
  Field(Field(config, "talker_config"), "num_hidden_layers").number--;
  Q4T_CHECK(!ValidateTtsBaseConfig(config));
  config = Config();
  config.object.emplace_back("backend_override", Json{});
  Q4T_CHECK(!ValidateTtsBaseConfig(config));
  config = Config();
  config.object.push_back(config.object.front());
  Q4T_CHECK(!ValidateTtsBaseConfig(config));
  return true;
}

Q4T_TEST(tts_base_config_does_not_accept_builtin_speaker_substitution) {
  auto config = Config();
  auto custom = Fixture("config-17b.json");
  Field(Field(config, "talker_config"), "spk_id") =
      Field(Field(custom, "talker_config"), "spk_id");
  Q4T_CHECK(!ValidateTtsBaseConfig(config));
  config = Config();
  Field(Field(config, "talker_config"), "codec_language_id") =
      Field(Field(custom, "talker_config"), "codec_language_id");
  Q4T_CHECK(!ValidateTtsBaseConfig(config));
  return true;
}

Q4T_TEST(tts_base_graph_matches_complete_independent_checkpoint) {
  const auto graph = TtsBaseTensorContracts();
  Q4T_CHECK(graph.size() == 480);
  Q4T_CHECK(std::count_if(graph.begin(), graph.end(), [](const auto& t) {
              return t.name.starts_with("speaker_encoder.");
            }) == 76);
  Q4T_CHECK(std::accumulate(
                graph.begin(), graph.end(), uint64_t{},
                [](auto n, const auto& t) { return n + t.bytes; }) == kBytes);
  Q4T_CHECK(ValidateTtsBaseTensorHeader(Header(), kBytes));
  return true;
}

Q4T_TEST(tts_base_graph_checks_every_speaker_dtype_and_shape) {
  const auto original = Header();
  for (const auto& [name, tensor] : original.object) {
    (void)tensor;
    if (!name.starts_with("speaker_encoder.")) continue;
    auto header = original;
    Field(Field(header, name), "dtype").str = "F16";
    Q4T_CHECK(!ValidateTtsBaseTensorHeader(header, kBytes));
    header = original;
    Field(Field(header, name), "shape").array[0].number++;
    Q4T_CHECK(!ValidateTtsBaseTensorHeader(header, kBytes));
  }
  return true;
}

Q4T_TEST(tts_base_graph_rejects_missing_extra_and_duplicate_tensors) {
  auto header = Header();
  std::erase_if(header.object,
                [](const auto& p) { return p.first == kSpeaker; });
  Q4T_CHECK(!ValidateTtsBaseTensorHeader(header, kBytes));
  header = Header();
  header.object.emplace_back("speaker_encoder.extra", Field(header, kSpeaker));
  Q4T_CHECK(!ValidateTtsBaseTensorHeader(header, kBytes));
  header = Header();
  header.object.emplace_back(kSpeaker, Field(header, kSpeaker));
  Q4T_CHECK(!ValidateTtsBaseTensorHeader(header, kBytes));
  return true;
}

Q4T_TEST(tts_base_graph_rejects_shifted_overlapping_and_trailing_storage) {
  for (const double shift : {-2, 2}) {
    auto header = Header();
    auto& offsets = Field(Field(header, kSpeaker), "data_offsets").array;
    offsets[0].number += shift;
    offsets[1].number += shift;
    Q4T_CHECK(!ValidateTtsBaseTensorHeader(header, kBytes));
  }
  Q4T_CHECK(!ValidateTtsBaseTensorHeader(Header(), kBytes - 2));
  Q4T_CHECK(!ValidateTtsBaseTensorHeader(Header(), kBytes + 2));
  return true;
}

Q4T_TEST(tts_base_graph_rejects_nonfinite_rank_and_nonintegral_extent) {
  auto header = Header();
  Field(Field(header, kSpeaker), "shape").array[0].number =
      std::numeric_limits<double>::infinity();
  Q4T_CHECK(!ValidateTtsBaseTensorHeader(header, kBytes));
  header = Header();
  Field(Field(header, kSpeaker), "shape").array.pop_back();
  Q4T_CHECK(!ValidateTtsBaseTensorHeader(header, kBytes));
  header = Header();
  Field(Field(header, kSpeaker), "data_offsets").array[0].number += 0.5;
  Q4T_CHECK(!ValidateTtsBaseTensorHeader(header, kBytes));
  return true;
}
