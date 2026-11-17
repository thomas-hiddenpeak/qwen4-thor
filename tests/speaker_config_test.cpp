#include "q4t/audio/speaker_config.h"
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
                         "fixtures/speaker" / name,
                     std::ios::binary);
  std::ostringstream text;
  text << file.rdbuf();
  Json value;
  if (!file || !q4t::io::ParseJson(text.str(), &value, false,
                                   {.reject_duplicate_keys = true}))
    throw std::runtime_error(std::string("speaker fixture: ") + name);
  return value;
}
Json& Member(Json& object, const std::string& name) {
  for (auto& [key, value] : object.object)
    if (key == name) return value;
  throw std::runtime_error("missing fixture member: " + name);
}
bool Rejected(const Json& header,
              uint64_t bytes = SpeakerConfig::kWeightBytes) {
  SpeakerTensorLayout layout;
  layout.offsets.fill(123);
  return !ParseSpeakerTensorLayout(header, bytes, &layout) &&
         std::all_of(layout.offsets.begin(), layout.offsets.end(),
                     [](size_t n) { return n == 123; });
}
}  // namespace

Q4T_TEST(speaker_config_pinned_identity_and_exact_fields) {
  const auto original = Fixture("config.json");
  Q4T_CHECK(ValidateSpeakerConfig(original));
  for (const char* name : {"profile", "model_id", "revision", "source_revision",
                           "weight_sha256"}) {
    auto value = original;
    Member(value, name).str += "changed";
    Q4T_CHECK(!ValidateSpeakerConfig(value));
  }
  auto value = original;
  value.object.push_back(value.object.front());
  Q4T_CHECK(!ValidateSpeakerConfig(value));
  value = original;
  value.object.back().first = "unknown";
  Q4T_CHECK(!ValidateSpeakerConfig(value));
  Q4T_CHECK(!ValidateSpeakerConfig(Json{}));
  return true;
}

Q4T_TEST(speaker_features_preserve_trained_frontend_not_asr_defaults) {
  const auto original = Fixture("config.json");
  for (const char* name : {"sample_rate", "mel_bins", "fft", "hop", "power",
                           "amin", "top_db"}) {
    auto value = original;
    Member(Member(value, "features"), name).number += 0.5;
    Q4T_CHECK(!ValidateSpeakerConfig(value));
  }
  for (const char* name : {"window", "padding", "mel_scale", "filter_shape"}) {
    auto value = original;
    Member(Member(value, "features"), name).str = "asr";
    Q4T_CHECK(!ValidateSpeakerConfig(value));
  }
  for (const char* name : {"mean_normalize", "std_normalize"}) {
    auto value = original;
    auto& flag = Member(Member(value, "features"), name);
    flag.boolean = !flag.boolean;
    Q4T_CHECK(!ValidateSpeakerConfig(value));
    flag.type = Json::Type::kNumber;
    Q4T_CHECK(!ValidateSpeakerConfig(value));
  }
  return true;
}

Q4T_TEST(speaker_embedding_preserves_graph_and_normalization) {
  const auto original = Fixture("config.json");
  for (const char* name : {"channels", "kernel_sizes", "dilations"}) {
    auto value = original;
    Member(Member(value, "embedding"), name).array.back().number++;
    Q4T_CHECK(!ValidateSpeakerConfig(value));
  }
  for (const char* name : {"res2_scale", "se_channels", "attention_channels",
                           "output_size", "norm_epsilon", "pool_epsilon"}) {
    auto value = original;
    Member(Member(value, "embedding"), name).number += 0.5;
    Q4T_CHECK(!ValidateSpeakerConfig(value));
  }
  for (const char* name : {"global_context", "global_embedding_norm"}) {
    auto value = original;
    auto& flag = Member(Member(value, "embedding"), name);
    flag.boolean = !flag.boolean;
    Q4T_CHECK(!ValidateSpeakerConfig(value));
  }
  for (const char* name : {"conv_padding", "activation"}) {
    auto value = original;
    Member(Member(value, "embedding"), name).str = "other";
    Q4T_CHECK(!ValidateSpeakerConfig(value));
  }
  return true;
}

Q4T_TEST(speaker_all_real_tensor_names_shapes_and_scalar_counters) {
  const auto header = Fixture("tensor-header.json");
  SpeakerTensorLayout layout;
  Q4T_CHECK(ParseSpeakerTensorLayout(header, SpeakerConfig::kWeightBytes,
                                      &layout));
  const auto tensors = SpeakerTensorContracts();
  Q4T_CHECK(tensors.size() == 231);
  size_t bytes = 0, counters = 0;
  for (size_t i = 0; i < tensors.size(); ++i) {
    Q4T_CHECK(layout.offsets[i] == bytes);
    bytes += tensors[i].bytes;
    if (tensors[i].dtype == "I64") {
      Q4T_CHECK(tensors[i].shape.empty() && tensors[i].bytes == 8);
      Q4T_CHECK(tensors[i].name.ends_with("num_batches_tracked"));
      ++counters;
    }
  }
  Q4T_CHECK(bytes == SpeakerConfig::kWeightBytes && counters == 31);
  return true;
}

Q4T_TEST(speaker_bad_names_dtypes_and_shapes_preserve_owner) {
  const auto original = Fixture("tensor-header.json");
  auto value = original;
  value.object.front().first = "unknown";
  Q4T_CHECK(Rejected(value));
  value = original;
  value.object.back().first = value.object.front().first;
  Q4T_CHECK(Rejected(value));
  value = original;
  value.object.pop_back();
  Q4T_CHECK(Rejected(value));
  for (const char* name : {"fc.conv.weight", "asp_bn.norm.num_batches_tracked"}) {
    value = original;
    Member(Member(value, name), "dtype").str = "BF16";
    Q4T_CHECK(Rejected(value));
    value = original;
    auto& shape = Member(Member(value, name), "shape");
    shape.array.push_back(Json{});
    Q4T_CHECK(Rejected(value));
  }
  value = original;
  Member(Member(value, "fc.conv.weight"), "shape").array[0].number += 0.5;
  Q4T_CHECK(Rejected(value));
  return true;
}

Q4T_TEST(speaker_storage_overlap_holes_fractional_and_overflow_rejected) {
  const auto original = Fixture("tensor-header.json");
  for (double bad : {-1., 0.5, 1e100,
                     std::numeric_limits<double>::infinity(),
                     std::numeric_limits<double>::quiet_NaN()}) {
    auto value = original;
    Member(Member(value, "fc.conv.weight"), "data_offsets").array[0].number =
        bad;
    Q4T_CHECK(Rejected(value));
  }
  auto value = original;
  Member(Member(value, "fc.conv.weight"), "data_offsets").array[1].number++;
  Q4T_CHECK(Rejected(value));
  Q4T_CHECK(Rejected(original, SpeakerConfig::kWeightBytes + 1));
  Q4T_CHECK(Rejected(original, std::numeric_limits<uint64_t>::max()));
  value = original;
  auto& item = Member(value, "fc.conv.weight");
  item.object.push_back(item.object.front());
  Q4T_CHECK(Rejected(value));
  return true;
}

Q4T_TEST(speaker_cancel_before_during_and_at_publication_is_atomic) {
  const auto header = Fixture("tensor-header.json");
  for (int stop_at : {1, 20, 233}) {
    SpeakerTensorLayout layout;
    layout.offsets.fill(456);
    int checks = 0;
    Q4T_CHECK(!ParseSpeakerTensorLayout(
        header, SpeakerConfig::kWeightBytes, &layout,
        [&] { return ++checks >= stop_at; }));
    Q4T_CHECK(std::all_of(layout.offsets.begin(), layout.offsets.end(),
                         [](size_t n) { return n == 456; }));
  }
  Q4T_CHECK(!ParseSpeakerTensorLayout(header, SpeakerConfig::kWeightBytes,
                                      nullptr));
  return true;
}
