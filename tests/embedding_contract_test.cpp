#include "q4t/model/embedding_config.h"
#include "q4t/test.h"
#include "q4t/text/tokenizer.h"

#include <cstdlib>
#include <fstream>
#include <set>
#include <sstream>

namespace {
using q4t::io::Json;
using q4t::text::Tokenizer;
using q4t::text::TokenizerLimits;
using q4t::text::TokenizerProfile;
constexpr auto kProfile = TokenizerProfile::kQwen3Embedding;

std::string Asset(const char* name) {
  const char* directory = std::getenv("Q4T_EMBEDDING_ASSETS");
  return std::string(directory ? directory
      : ".q4t-work/native-embedding-20261111/assets") + "/" + name;
}

std::string Read(const std::string& path) {
  std::ifstream file(path, std::ios::binary);
  std::ostringstream buffer;
  buffer << file.rdbuf();
  return buffer.str();
}

const std::string& TokenizerJson() {
  static const std::string value = Read(Asset("tokenizer.json"));
  return value;
}

std::unique_ptr<Tokenizer> Load(TokenizerLimits limits = {}) {
  std::unique_ptr<Tokenizer> tokenizer;
  if (!Tokenizer::LoadJson(TokenizerJson(), limits, &tokenizer, kProfile))
    return nullptr;
  return tokenizer;
}

bool Replace(std::string* text, const std::string& from, const std::string& to) {
  const auto start = text->find(from);
  if (start == std::string::npos) return false;
  text->replace(start, from.size(), to);
  return true;
}
}  // namespace

Q4T_TEST(embedding_tokenizer_matches_reference_ids_with_trained_eos) {
  auto tokenizer = Load();
  Q4T_CHECK(tokenizer && tokenizer->BaseVocabSize() == 151643 &&
            tokenizer->MergeCount() == 151387 &&
            tokenizer->VocabSize() == 151669);
  Json golden;
  Q4T_CHECK(q4t::io::ParseJson(
      Read("tests/fixtures/embedding/tokenizer-golden.json"), &golden));
  const auto* cases = golden.GetArray("cases");
  Q4T_CHECK(cases && cases->array.size() == 14);
  for (const auto& row : cases->array) {
    for (const bool special : {true, false}) {
      std::vector<uint32_t> ids;
      Q4T_CHECK(tokenizer->Encode(row.GetString("text"), &ids,
                                 {.add_special_tokens = special}));
      const auto* expected = row.GetArray(special ? "ids" : "raw_ids");
      Q4T_CHECK(expected && ids.size() == expected->array.size());
      for (size_t i = 0; i < ids.size(); ++i)
        Q4T_CHECK(ids[i] == expected->array[i].AsInt(-1));
      std::string decoded;
      Q4T_CHECK(tokenizer->Decode(ids, false, &decoded));
      if (special) Q4T_CHECK(decoded.ends_with("<|endoftext|>"));
    }
  }
  return true;
}

Q4T_TEST(embedding_tokenizer_profile_does_not_relax_qwen4_default) {
  std::unique_ptr<Tokenizer> tokenizer;
  Q4T_CHECK(!Tokenizer::LoadJson(TokenizerJson(), {}, &tokenizer));
  Q4T_CHECK(!tokenizer);
  const std::string original =
      "/home/rm01/models/dev/llm/garnermccloud/"
      "Qwen3.8-Flash-Next-NVFP4-SSD-Stream/tokenizer.json";
  Q4T_CHECK(!Tokenizer::Load(original, {}, &tokenizer, kProfile));
  Q4T_CHECK(Tokenizer::Load(original, {}, &tokenizer));
  std::vector<uint32_t> ids;
  Q4T_CHECK(tokenizer->Encode("Hello, world!", &ids));
  Q4T_CHECK(ids == std::vector<uint32_t>({9419, 11, 1814, 0}));
  Q4T_CHECK(!Tokenizer::LoadJson(TokenizerJson(), {}, &tokenizer,
                                static_cast<TokenizerProfile>(99)));
  Q4T_CHECK(tokenizer->Encode("!", &ids) && ids == std::vector<uint32_t>({0}));
  return true;
}

Q4T_TEST(embedding_tokenizer_eos_budget_and_cancellation_are_atomic) {
  TokenizerLimits limits;
  limits.max_tokens = 1;
  auto tokenizer = Load(limits);
  Q4T_CHECK(tokenizer);
  std::vector<uint32_t> ids;
  Q4T_CHECK(tokenizer->Encode("", &ids) && ids == std::vector<uint32_t>({151643}));
  Q4T_CHECK(!tokenizer->Encode("!", &ids) && ids.empty());
  Q4T_CHECK(tokenizer->Encode("!", &ids, {.add_special_tokens = false}) &&
            ids == std::vector<uint32_t>({0}));
  Q4T_CHECK(!tokenizer->Encode("!", &ids, {.cancelled = [] { return true; }}) &&
            ids.empty());
  return true;
}

Q4T_TEST(embedding_tokenizer_rejects_wrong_eos_and_fractional_ids) {
  std::unique_ptr<Tokenizer> tokenizer;
  // The actual asset's EOS metadata is part of the trained representation.
  auto wrong_eos = TokenizerJson();
  Q4T_CHECK(Replace(&wrong_eos, "\"ids\": [\n              151643",
                    "\"ids\": [\n              151644"));
  Q4T_CHECK(!Tokenizer::LoadJson(wrong_eos, {}, &tokenizer, kProfile));
  auto fractional = TokenizerJson();
  Q4T_CHECK(Replace(&fractional, "\"id\": 151643,", "\"id\": 151643.5,"));
  Q4T_CHECK(!Tokenizer::LoadJson(fractional, {}, &tokenizer, kProfile));
  auto duplicate = TokenizerJson();
  Q4T_CHECK(Replace(&duplicate, "\"version\": \"1.0\",",
                    "\"version\": \"1.0\", \"version\": \"1.0\","));
  Q4T_CHECK(!Tokenizer::LoadJson(duplicate, {}, &tokenizer, kProfile));
  return true;
}

Q4T_TEST(embedding_architecture_rejects_wrong_compute_contract) {
  Json config;
  Q4T_CHECK(q4t::io::ParseJson(Read(Asset("config.json")), &config));
  Q4T_CHECK(q4t::model::ValidateEmbeddingConfig(config));
  for (const char* field : {"hidden_size", "num_hidden_layers", "rope_theta",
                            "rms_norm_eps", "eos_token_id"}) {
    auto changed = config;
    for (auto& [name, value] : changed.object)
      if (name == field) value.number += 1;
    Q4T_CHECK(!q4t::model::ValidateEmbeddingConfig(changed));
  }
  for (const char* field : {"attention_bias", "tie_word_embeddings"}) {
    auto changed = config;
    for (auto& [name, value] : changed.object)
      if (name == field) value.boolean = !value.boolean;
    Q4T_CHECK(!q4t::model::ValidateEmbeddingConfig(changed));
  }
  return true;
}

Q4T_TEST(embedding_tensor_contract_pins_actual_names_shapes_and_bytes) {
  Json header;
  Q4T_CHECK(q4t::io::ParseJson(
      Read(".q4t-work/native-embedding-20261111/tensor-header.json"), &header));
  const auto contracts = q4t::model::EmbeddingTensorContracts();
  Q4T_CHECK(contracts.size() == 310 && header.object.size() == 311);
  size_t bytes = 0;
  std::set<std::string> names;
  for (const auto& tensor : contracts) {
    Q4T_CHECK(names.insert(tensor.name).second);
    const auto* actual = header.Find(tensor.name);
    Q4T_CHECK(actual && actual->GetString("dtype") == "BF16");
    const auto* shape = actual->GetArray("shape");
    const auto* offsets = actual->GetArray("data_offsets");
    Q4T_CHECK(shape && shape->array.size() == tensor.shape.size() &&
              offsets && offsets->array.size() == 2);
    for (size_t i = 0; i < tensor.shape.size(); ++i)
      Q4T_CHECK(shape->array[i].AsInt() == tensor.shape[i]);
    Q4T_CHECK(static_cast<size_t>(offsets->array[1].AsInt() -
                                  offsets->array[0].AsInt()) == tensor.bytes);
    bytes += tensor.bytes;
  }
  Q4T_CHECK(bytes == q4t::model::EmbeddingWeightBytes());
  return true;
}
