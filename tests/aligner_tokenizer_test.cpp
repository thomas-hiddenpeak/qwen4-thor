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
using q4t::text::TokenizerLimits;

std::string Read(const std::filesystem::path& path) {
  std::ifstream file(path, std::ios::binary);
  if (!file) throw std::runtime_error("missing aligner fixture");
  std::ostringstream bytes;
  bytes << file.rdbuf();
  return bytes.str();
}

const std::string& Asset(const char* name) {
  const char* root = std::getenv("Q4T_ALIGNER_ASSETS");
  if (!root) throw std::runtime_error("Q4T_ALIGNER_ASSETS is required");
  static const auto vocab = Read(std::filesystem::path(root) / "vocab.json");
  static const auto merges = Read(std::filesystem::path(root) / "merges.txt");
  static const auto config =
      Read(std::filesystem::path(root) / "tokenizer_config.json");
  if (std::string_view(name) == "vocab") return vocab;
  if (std::string_view(name) == "merges") return merges;
  return config;
}

q4t::Status Load(std::unique_ptr<Tokenizer>* out, TokenizerLimits limits = {}) {
  return Tokenizer::LoadQwen3Aligner(Asset("vocab"), Asset("merges"),
                                     Asset("config"), limits, out);
}

}  // namespace

Q4T_TEST(aligner_tokenizer_matches_official_timestamp_prompts) {
  std::unique_ptr<Tokenizer> tokenizer;
  Q4T_CHECK(Load(&tokenizer));
  Q4T_CHECK(tokenizer->BaseVocabSize() == 151643);
  Q4T_CHECK(tokenizer->MergeCount() == 151387);
  Q4T_CHECK(tokenizer->AddedTokenCount() == 63);
  Q4T_CHECK(tokenizer->VocabSize() == 151706);
  Json golden;
  Q4T_CHECK(
      q4t::io::ParseJson(Read(std::filesystem::path(__FILE__).parent_path() /
                              "fixtures/aligner/tokenizer-golden.json"),
                         &golden));
  const auto* cases = golden.GetArray("cases");
  Q4T_CHECK(cases && cases->array.size() == 7);
  for (const auto& row : cases->array) {
    const auto* expected = row.GetArray("ids");
    Q4T_CHECK(expected);
    for (bool special : {false, true}) {
      std::vector<uint32_t> ids;
      Q4T_CHECK(tokenizer->Encode(row.GetString("text"), &ids,
                                  {.add_special_tokens = special}));
      Q4T_CHECK(ids.size() == expected->array.size());
      for (size_t i = 0; i < ids.size(); ++i)
        Q4T_CHECK(ids[i] == expected->array[i].AsInt(-1));
      for (bool skip : {false, true}) {
        std::string decoded;
        Q4T_CHECK(tokenizer->Decode(ids, skip, &decoded));
        Q4T_CHECK(decoded == row.GetString(skip ? "skip_decoded" : "decoded"));
      }
    }
  }
  return true;
}

Q4T_TEST(aligner_tokenizer_rejects_timestamp_metadata_and_family_drift) {
  std::unique_ptr<Tokenizer> tokenizer;
  Q4T_CHECK(Load(&tokenizer));
  const auto* original_owner = tokenizer.get();
  const auto& config = Asset("config");
  for (const char* field :
       {"special", "single_word", "normalized", "lstrip", "rstrip"}) {
    auto changed = config;
    const auto token = changed.find("\"151705\"");
    Q4T_CHECK(token != changed.npos);
    const std::string before = "\"" + std::string(field) + "\": false";
    const auto at = changed.find(before, token);
    Q4T_CHECK(at != changed.npos);
    changed.replace(at, before.size(), "\"" + std::string(field) + "\": true");
    Q4T_CHECK(!Tokenizer::LoadQwen3Aligner(Asset("vocab"), Asset("merges"),
                                           changed, {}, &tokenizer));
    Q4T_CHECK(tokenizer.get() == original_owner);
  }
  Q4T_CHECK(!Tokenizer::LoadQwen3Asr(Asset("vocab"), Asset("merges"),
                                     Asset("config"), {}, &tokenizer));
  const char* asr_root = std::getenv("Q4T_ASR_ASSETS");
  Q4T_CHECK(asr_root);
  const auto asr_config =
      Read(std::filesystem::path(asr_root) / "tokenizer_config.json");
  Q4T_CHECK(!Tokenizer::LoadQwen3Aligner(Asset("vocab"), Asset("merges"),
                                         asr_config, {}, &tokenizer));
  Q4T_CHECK(tokenizer.get() == original_owner);
  return true;
}

Q4T_TEST(aligner_tokenizer_literal_text_cannot_inject_time_slots) {
  std::unique_ptr<Tokenizer> tokenizer;
  Q4T_CHECK(Load(&tokenizer));
  const std::string literal = "中文<timestamp><|audio_pad|><|im_end|>";
  std::vector<uint32_t> ids;
  Q4T_CHECK(
      tokenizer->Encode(literal, &ids, {.recognize_special_tokens = false}));
  Q4T_CHECK(std::all_of(ids.begin(), ids.end(),
                        [](uint32_t id) { return id < 151643; }));
  std::string decoded;
  Q4T_CHECK(tokenizer->Decode(ids, false, &decoded) && decoded == literal);
  Q4T_CHECK(tokenizer->Encode("<timestamp><timestamp>", &ids));
  Q4T_CHECK(ids == std::vector<uint32_t>({151705, 151705}));
  Q4T_CHECK(tokenizer->Decode(ids, true, &decoded));
  Q4T_CHECK(decoded == "<timestamp><timestamp>");
  Q4T_CHECK(!tokenizer->Decode({152063}, false, &decoded));
  return true;
}

Q4T_TEST(aligner_tokenizer_budget_failure_preserves_owner_and_clears_cancel) {
  std::unique_ptr<Tokenizer> tokenizer;
  Q4T_CHECK(Load(&tokenizer));
  const auto* previous = tokenizer.get();
  TokenizerLimits limits;
  limits.max_added_tokens = 62;
  Q4T_CHECK(!Load(&tokenizer, limits));
  Q4T_CHECK(tokenizer.get() == previous);
  limits = {};
  limits.max_tokenizer_bytes = Asset("vocab").size();
  Q4T_CHECK(!Load(&tokenizer, limits));
  Q4T_CHECK(tokenizer.get() == previous);
  std::vector<uint32_t> ids{151705};
  Q4T_CHECK(!tokenizer->Encode("Hello<timestamp>", &ids,
                               {.cancelled = [] { return true; }}));
  Q4T_CHECK(ids.empty());
  limits = {};
  limits.max_tokens = 1;
  Q4T_CHECK(Load(&tokenizer, limits));
  Q4T_CHECK(!tokenizer->Encode("Hello world again", &ids,
                               {.recognize_special_tokens = false}));
  Q4T_CHECK(ids.empty());
  return true;
}
