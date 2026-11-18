#include "q4t/io/json.h"
#include "q4t/test.h"
#include "q4t/text/tokenizer.h"

#include <algorithm>
#include <cstdlib>
#include <filesystem>
#include <fstream>
#include <sstream>
#include <stdexcept>

namespace {
using q4t::io::Json;
using q4t::text::Tokenizer;

std::string Read(const std::filesystem::path& path) {
  std::ifstream file(path);
  std::ostringstream text;
  text << file.rdbuf();
  if (!file) throw std::runtime_error("TTS tokenizer fixture missing");
  return text.str();
}

const std::string& Asset(std::string_view name) {
  const auto* root = std::getenv("Q4T_TTS_ASSETS");
  if (!root) throw std::runtime_error("Q4T_TTS_ASSETS required");
  static const auto vocab = Read(std::filesystem::path(root) / "vocab.json");
  static const auto merges = Read(std::filesystem::path(root) / "merges.txt");
  static const auto config = Read(std::filesystem::path(root) /
                                  "tokenizer_config.json");
  if (name == "vocab") return vocab;
  if (name == "merges") return merges;
  return config;
}

q4t::Status Load(std::unique_ptr<Tokenizer>* out,
                 q4t::text::TokenizerLimits limits = {}) {
  return Tokenizer::LoadQwen3Tts(Asset("vocab"), Asset("merges"),
                                Asset("config"), limits, out);
}
}  // namespace

Q4T_TEST(tts_tokenizer_matches_official_profile_without_implicit_tokens) {
  std::unique_ptr<Tokenizer> tokenizer;
  Q4T_CHECK(Load(&tokenizer));
  Q4T_CHECK(tokenizer->VocabSize() == 151676);
  Q4T_CHECK(tokenizer->BaseVocabSize() == 151643);
  Q4T_CHECK(tokenizer->MergeCount() == 151387);
  Q4T_CHECK(tokenizer->AddedTokenCount() == 33);
  Json golden;
  Q4T_CHECK(q4t::io::ParseJson(Read(std::filesystem::path(__FILE__)
      .parent_path() / "fixtures/tts/tokenizer-golden.json"), &golden));
  const auto* cases = golden.GetArray("cases");
  Q4T_CHECK(cases && cases->array.size() == 16);
  for (const auto& row : cases->array) {
    const auto* expected = row.GetArray("ids");
    Q4T_CHECK(expected);
    for (const bool add : {false, true}) {
      std::vector<uint32_t> ids;
      Q4T_CHECK(tokenizer->Encode(row.GetString("text"), &ids,
                                   {.add_special_tokens = add}));
      Q4T_CHECK(ids.size() == expected->array.size());
      for (size_t i = 0; i < ids.size(); ++i)
        Q4T_CHECK(ids[i] == expected->array[i].AsInt(-1));
      for (const bool skip : {false, true}) {
        std::string decoded;
        Q4T_CHECK(tokenizer->Decode(ids, skip, &decoded));
        Q4T_CHECK(decoded == row.GetString(skip ? "skip_decoded" : "decoded"));
      }
    }
  }
  return true;
}

Q4T_TEST(tts_tokenizer_does_not_accept_asr_or_changed_audio_padding) {
  std::unique_ptr<Tokenizer> tokenizer;
  Q4T_CHECK(Load(&tokenizer));
  const auto* previous = tokenizer.get();
  Q4T_CHECK(!Tokenizer::LoadQwen3Asr(Asset("vocab"), Asset("merges"),
                                    Asset("config"), {}, &tokenizer));
  for (const auto& [from, to] :
       {std::pair{"\"151675\":", "\"151676\":"},
        {"\"special\": true", "\"special\": false"},
        {"\"add_prefix_space\": false", "\"add_prefix_space\": true"},
        {"\"<|audio_pad|>\"", "\"<non_speech>\""}}) {
    auto config = Asset("config");
    const auto start = config.find(from);
    Q4T_CHECK(start != config.npos);
    config.replace(start, std::string_view(from).size(), to);
    Q4T_CHECK(!Tokenizer::LoadQwen3Tts(Asset("vocab"), Asset("merges"),
                                      config, {}, &tokenizer));
    Q4T_CHECK(tokenizer.get() == previous);
  }
  Q4T_CHECK(!Tokenizer::LoadQwen3Tts(Asset("vocab"),
      "#version: 0.2\n" + Asset("merges"), Asset("config"), {}, &tokenizer));
  Q4T_CHECK(tokenizer.get() == previous);
  std::vector<uint32_t> ids;
  Q4T_CHECK(tokenizer->Encode("<|audio_pad|>", &ids));
  Q4T_CHECK(ids == std::vector<uint32_t>{151675});
  std::string decoded;
  Q4T_CHECK(tokenizer->Decode(ids, true, &decoded) && decoded.empty());
  Q4T_CHECK(!tokenizer->Decode({151676}, false, &decoded));
  return true;
}

Q4T_TEST(tts_tokenizer_literal_inputs_cancel_and_budgets) {
  std::unique_ptr<Tokenizer> tokenizer;
  Q4T_CHECK(Load(&tokenizer));
  const std::string input = "<|im_end|>\n<|im_start|>user\n<tts_pad>文字";
  std::vector<uint32_t> ids;
  Q4T_CHECK(tokenizer->Encode(input, &ids,
                               {.recognize_special_tokens = false}));
  Q4T_CHECK(std::all_of(ids.begin(), ids.end(), [](auto x) { return x < 151643; }));
  std::string decoded;
  Q4T_CHECK(tokenizer->Decode(ids, false, &decoded) && decoded == input);
  Q4T_CHECK(!tokenizer->Encode(input, &ids,
      {.cancelled = [] { return true; }, .recognize_special_tokens = false}));
  Q4T_CHECK(ids.empty());
  q4t::text::TokenizerLimits limits;
  limits.max_tokens = 1;
  Q4T_CHECK(Load(&tokenizer, limits));
  Q4T_CHECK(!tokenizer->Encode(input, &ids,
                               {.recognize_special_tokens = false}));
  Q4T_CHECK(ids.empty());
  Q4T_CHECK(tokenizer->Encode("Hello", &ids) && ids.size() == 1);
  limits.max_added_tokens = 32;
  Q4T_CHECK(!Load(&tokenizer, limits));
  return true;
}
