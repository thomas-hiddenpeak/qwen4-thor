#include "q4t/audio/separator_config.h"
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
Json Fixture(const char* name) {
  std::ifstream file(std::filesystem::path(__FILE__).parent_path() /
                         "fixtures/separator" / name,
                     std::ios::binary);
  std::ostringstream text;
  text << file.rdbuf();
  Json value;
  if (!file || !q4t::io::ParseJson(text.str(), &value, false,
                                   {.reject_duplicate_keys = true}))
    throw std::runtime_error(std::string("separator fixture: ") + name);
  return value;
}
Json& Member(Json& object, const std::string& name) {
  for (auto& [key, value] : object.object)
    if (key == name) return value;
  throw std::runtime_error("missing separator member: " + name);
}
bool Rejected(const Json& header,
              uint64_t bytes = SeparatorConfig::kWeightBytes) {
  SeparatorTensorLayout layout;
  layout.offsets.fill(123);
  return !ParseSeparatorTensorLayout(header, bytes, &layout) &&
         std::all_of(layout.offsets.begin(), layout.offsets.end(),
                     [](size_t n) { return n == 123; });
}
bool RejectMutations(Json* value, Json* whole) {
  for (auto& [key, field] : value->object) {
    (void)key;
    if (field.IsObject()) {
      if (!RejectMutations(&field, whole)) return false;
      continue;
    }
    const auto original = field;
    if (field.IsString()) field.str += "changed";
    if (field.IsNumber()) field.number += 0.25;
    if (field.IsBool()) field.boolean = !field.boolean;
    const bool rejected = !ValidateSeparatorConfig(*whole);
    field = original;
    if (!rejected) return false;
  }
  value->object.push_back(value->object.front());
  const bool rejected = !ValidateSeparatorConfig(*whole);
  value->object.pop_back();
  return rejected;
}
}  // namespace

Q4T_TEST(separator_config_fixed_identity_and_trained_graph) {
  auto config = Fixture("config.json");
  Q4T_CHECK(ValidateSeparatorConfig(config));
  Q4T_CHECK(RejectMutations(&config, &config));
  Q4T_CHECK(ValidateSeparatorConfig(config));
  auto bad = config;
  bad.object.pop_back();
  Q4T_CHECK(!ValidateSeparatorConfig(bad));
  Q4T_CHECK(!ValidateSeparatorConfig(Json{}));
  return true;
}

Q4T_TEST(separator_tensor_contract_complete_real_header) {
  const auto header = Fixture("tensor-header.json");
  const auto specs = SeparatorTensorContracts();
  Q4T_CHECK(specs.size() == 417);
  SeparatorTensorLayout layout;
  Q4T_CHECK(ParseSeparatorTensorLayout(header, 112957444, &layout));
  size_t end = 0;
  for (size_t i = 0; i < specs.size(); ++i) {
    Q4T_CHECK(layout.offsets[i] == end);
    end += specs[i].bytes;
    Q4T_CHECK(i == 0 || specs[i - 1].name < specs[i].name);
  }
  Q4T_CHECK(end == 112957444);
  return true;
}

Q4T_TEST(separator_tensor_names_missing_duplicate_unknown) {
  const auto header = Fixture("tensor-header.json");
  auto bad = header;
  bad.object.pop_back();
  Q4T_CHECK(Rejected(bad));
  bad = header;
  bad.object.back() = bad.object.front();
  Q4T_CHECK(Rejected(bad));
  bad = header;
  bad.object.back().first = "masknet.untrained_extra";
  Q4T_CHECK(Rejected(bad));
  Q4T_CHECK(Rejected(Json{}));
  return true;
}

Q4T_TEST(separator_tensor_dtype_shape_and_metadata) {
  const auto header = Fixture("tensor-header.json");
  for (const auto* name : {"decoder.weight", "masknet.prelu.weight",
                           "masknet.dual_mdl.1.inter_mdl.pos_enc.pe"}) {
    auto bad = header;
    Member(Member(bad, name), "dtype").str = "BF16";
    Q4T_CHECK(Rejected(bad));
    bad = header;
    Member(Member(bad, name), "shape").array.back().number++;
    Q4T_CHECK(Rejected(bad));
    bad = header;
    auto& tensor = Member(bad, name);
    tensor.object.back().first = "unknown";
    Q4T_CHECK(Rejected(bad));
    bad = header;
    Member(Member(bad, name), "shape").array.clear();
    Q4T_CHECK(Rejected(bad));
  }
  return true;
}

Q4T_TEST(separator_tensor_storage_intervals_and_byte_limits) {
  const auto header = Fixture("tensor-header.json");
  for (double value : {-1., 0.5, 16385., 1e30,
                        std::numeric_limits<double>::infinity(),
                        std::numeric_limits<double>::quiet_NaN()}) {
    for (size_t i = 0; i < 2; ++i) {
      auto bad = header;
      Member(Member(bad, "encoder.conv1d.weight"), "data_offsets")
          .array[i].number = value;
      Q4T_CHECK(Rejected(bad));
    }
  }
  Q4T_CHECK(Rejected(header, 112957443));
  Q4T_CHECK(Rejected(header, 112957445));
  Q4T_CHECK(Rejected(header, std::numeric_limits<uint64_t>::max()));
  auto bad = header;
  auto& tensor = bad.object.front().second;
  tensor.object.push_back(tensor.object.back());
  Q4T_CHECK(Rejected(bad));
  return true;
}

Q4T_TEST(separator_tensor_cancellation_and_output_atomicity) {
  const auto header = Fixture("tensor-header.json");
  for (int threshold : {1, 211, 419}) {
    int calls = 0;
    SeparatorTensorLayout layout;
    layout.offsets.fill(123);
    Q4T_CHECK(!ParseSeparatorTensorLayout(
        header, SeparatorConfig::kWeightBytes, &layout,
        [&] { return ++calls >= threshold; }));
    Q4T_CHECK(std::all_of(layout.offsets.begin(), layout.offsets.end(),
                           [](size_t n) { return n == 123; }));
  }
  Q4T_CHECK(!ParseSeparatorTensorLayout(header, 112957444, nullptr));
  return true;
}

Q4T_TEST(separator_encoder_decoder_paths_and_position_buffers) {
  const auto specs = SeparatorTensorContracts();
  const auto get = [&](const std::string& name) -> const auto& {
    const auto it = std::find_if(specs.begin(), specs.end(),
                                 [&](const auto& x) { return x.name == name; });
    if (it == specs.end()) throw std::runtime_error(name);
    return *it;
  };
  Q4T_CHECK(get("encoder.conv1d.weight").bytes == 16384);
  Q4T_CHECK(get("decoder.weight").shape ==
             std::vector<int64_t>({256, 1, 16}));
  for (int block = 0; block < 2; ++block) {
    for (const auto* path : {"intra", "inter"}) {
      const auto base = "masknet.dual_mdl." + std::to_string(block) + "." +
                        path + "_mdl";
      Q4T_CHECK(get(base + ".pos_enc.pe").bytes == 2560000);
      for (int layer = 0; layer < 8; ++layer) {
        const auto name = base + ".mdl.layers." + std::to_string(layer);
        Q4T_CHECK(get(name + ".self_att.att.in_proj_weight").shape ==
                   std::vector<int64_t>({768, 256}));
        Q4T_CHECK(get(name + ".pos_ffn.ffn.3.weight").shape ==
                   std::vector<int64_t>({256, 1024}));
      }
    }
  }
  return true;
}
