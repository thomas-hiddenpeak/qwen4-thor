#include "q4t/audio/speaker_config.h"

#include <algorithm>
#include <cmath>
#include <initializer_list>
#include <set>
#include <string_view>

namespace q4t::audio {
namespace {
bool Fields(const io::Json& value,
            std::initializer_list<std::string_view> names) {
  if (!value.IsObject() || value.object.size() != names.size()) return false;
  std::set<std::string_view> seen;
  for (const auto& [name, unused] : value.object) {
    (void)unused;
    if (std::find(names.begin(), names.end(), name) == names.end() ||
        !seen.insert(name).second)
      return false;
  }
  return true;
}
bool Number(const io::Json* value, double expected) {
  return value && value->IsNumber() && std::isfinite(value->number) &&
         value->number == expected;
}
bool Text(const io::Json& value, const char* name, std::string_view expected) {
  const auto* found = value.Find(name);
  return found && found->IsString() && found->str == expected;
}
bool Array(const io::Json* value, std::initializer_list<int> expected) {
  if (!value || !value->IsArray() || value->array.size() != expected.size())
    return false;
  size_t i = 0;
  for (int number : expected)
    if (!Number(&value->array[i++], number)) return false;
  return true;
}
}  // namespace

std::vector<SpeakerTensorContract> SpeakerTensorContracts() {
  std::vector<SpeakerTensorContract> result;
  const auto add = [&](std::string name, std::vector<int64_t> shape,
                       std::string dtype = "F32") {
    size_t bytes = dtype == "F32" ? 4 : 8;
    for (auto dim : shape) bytes *= dim;
    result.push_back({std::move(name), std::move(dtype), std::move(shape),
                      bytes});
  };
  const auto conv = [&](const std::string& name, int in, int out, int kernel) {
    add(name + ".conv.weight", {out, in, kernel});
    add(name + ".conv.bias", {out});
  };
  const auto norm = [&](const std::string& name, int channels) {
    for (const char* key : {"weight", "bias", "running_mean", "running_var"})
      add(name + ".norm." + key, {channels});
    add(name + ".norm.num_batches_tracked", {}, "I64");
  };
  const auto tdnn = [&](const std::string& name, int in, int out, int kernel) {
    conv(name + ".conv", in, out, kernel);
    norm(name + ".norm", out);
  };
  tdnn("blocks.0", 80, 1024, 5);
  for (int layer = 1; layer <= 3; ++layer) {
    const std::string name = "blocks." + std::to_string(layer);
    tdnn(name + ".tdnn1", 1024, 1024, 1);
    for (int block = 0; block < 7; ++block)
      tdnn(name + ".res2net_block.blocks." + std::to_string(block), 128,
           128, 3);
    tdnn(name + ".tdnn2", 1024, 1024, 1);
    conv(name + ".se_block.conv1", 1024, 128, 1);
    conv(name + ".se_block.conv2", 128, 1024, 1);
  }
  tdnn("mfa", 3072, 3072, 1);
  tdnn("asp.tdnn", 9216, 128, 1);
  conv("asp.conv", 128, 3072, 1);
  norm("asp_bn", 6144);
  conv("fc", 6144, 192, 1);
  std::sort(result.begin(), result.end(), [](const auto& a, const auto& b) {
    return a.name < b.name;
  });
  return result;
}

Status ValidateSpeakerConfig(const io::Json& config) {
  if (!Fields(config, {"profile", "model_id", "revision", "source_revision",
                       "weight_sha256", "features", "embedding"}) ||
      !Text(config, "profile", SpeakerConfig::kProfile) ||
      !Text(config, "model_id", SpeakerConfig::kModelId) ||
      !Text(config, "revision", SpeakerConfig::kRevision) ||
      !Text(config, "source_revision", SpeakerConfig::kSourceRevision) ||
      !Text(config, "weight_sha256", SpeakerConfig::kWeightSha256))
    return Status::Fail("Speaker model identity differs");
  const auto& features = *config.Find("features");
  if (!Fields(features, {"sample_rate", "mel_bins", "fft", "hop", "window",
                         "padding", "mel_scale", "filter_shape", "power",
                         "amin", "top_db", "mean_normalize", "std_normalize"}) ||
      !Number(features.Find("sample_rate"), 16000) ||
      !Number(features.Find("mel_bins"), 80) ||
      !Number(features.Find("fft"), 400) ||
      !Number(features.Find("hop"), 160) ||
      !Text(features, "window", "periodic_hamming") ||
      !Text(features, "padding", "center_constant") ||
      !Text(features, "mel_scale", "htk") ||
      !Text(features, "filter_shape", "symmetric_left_band_triangle") ||
      !Number(features.Find("power"), 2) ||
      !Number(features.Find("amin"), 1e-10) ||
      !Number(features.Find("top_db"), 80) ||
      !features.Find("mean_normalize")->IsBool() ||
      !features.Find("mean_normalize")->boolean ||
      !features.Find("std_normalize")->IsBool() ||
      features.Find("std_normalize")->boolean)
    return Status::Fail("Speaker trained feature contract differs");
  const auto& model = *config.Find("embedding");
  if (!Fields(model, {"channels", "kernel_sizes", "dilations", "res2_scale",
                      "se_channels", "attention_channels", "global_context",
                      "output_size", "norm_epsilon", "pool_epsilon",
                      "conv_padding", "activation", "global_embedding_norm"}) ||
      !Array(model.Find("channels"), {1024, 1024, 1024, 1024, 3072}) ||
      !Array(model.Find("kernel_sizes"), {5, 3, 3, 3, 1}) ||
      !Array(model.Find("dilations"), {1, 2, 3, 4, 1}) ||
      !Number(model.Find("res2_scale"), 8) ||
      !Number(model.Find("se_channels"), 128) ||
      !Number(model.Find("attention_channels"), 128) ||
      !model.Find("global_context")->IsBool() ||
      !model.Find("global_context")->boolean ||
      !Number(model.Find("output_size"), 192) ||
      !Number(model.Find("norm_epsilon"), 1e-5) ||
      !Number(model.Find("pool_epsilon"), 1e-12) ||
      !Text(model, "conv_padding", "reflect") ||
      !Text(model, "activation", "relu") ||
      !model.Find("global_embedding_norm")->IsBool() ||
      model.Find("global_embedding_norm")->boolean)
    return Status::Fail("Speaker trained embedding contract differs");
  return {};
}

Status ParseSpeakerTensorLayout(const io::Json& header, uint64_t data_bytes,
                                SpeakerTensorLayout* out,
                                std::function<bool()> cancelled) {
  const auto interrupted = [&] { return cancelled && cancelled(); };
  if (interrupted()) return Status::Fail("Speaker layout cancelled");
  if (!out || !header.IsObject() ||
      header.object.size() != SpeakerConfig::kTensors ||
      data_bytes != SpeakerConfig::kWeightBytes)
    return Status::Fail("Speaker tensor map or byte count differs");
  const auto contracts = SpeakerTensorContracts();
  std::set<std::string_view> names;
  for (const auto& [name, unused] : header.object) {
    (void)unused;
    if (!names.insert(name).second)
      return Status::Fail("Speaker tensor name duplicated");
  }
  SpeakerTensorLayout layout;
  size_t offset = 0;
  for (size_t i = 0; i < contracts.size(); ++i) {
    if (interrupted()) return Status::Fail("Speaker layout cancelled");
    const auto& spec = contracts[i];
    const auto* value = header.Find(spec.name);
    if (!value || !Fields(*value, {"dtype", "shape", "data_offsets"}) ||
        !Text(*value, "dtype", spec.dtype))
      return Status::Fail("Speaker tensor name or dtype differs");
    const auto* shape = value->GetArray("shape");
    const auto* bounds = value->GetArray("data_offsets");
    if (!shape || shape->array.size() != spec.shape.size() || !bounds ||
        bounds->array.size() != 2)
      return Status::Fail("Speaker tensor rank or storage bounds differ");
    for (size_t j = 0; j < spec.shape.size(); ++j)
      if (!Number(&shape->array[j], spec.shape[j]))
        return Status::Fail("Speaker tensor dimension differs");
    if (!Number(&bounds->array[0], offset) ||
        !Number(&bounds->array[1], offset + spec.bytes))
      return Status::Fail("Speaker tensor storage is not canonical");
    layout.offsets[i] = offset;
    offset += spec.bytes;
  }
  if (offset != data_bytes || interrupted())
    return Status::Fail("Speaker tensor layout incomplete or cancelled");
  *out = layout;
  return {};
}
}  // namespace q4t::audio
