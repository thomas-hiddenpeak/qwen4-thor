#include "q4t/audio/separator_config.h"

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
bool Flag(const io::Json& value, const char* name, bool expected) {
  const auto* found = value.Find(name);
  return found && found->IsBool() && found->boolean == expected;
}
}  // namespace

std::vector<SeparatorTensorContract> SeparatorTensorContracts() {
  std::vector<SeparatorTensorContract> result;
  const auto add = [&](std::string name, std::vector<int64_t> shape) {
    size_t bytes = sizeof(float);
    for (auto dim : shape) bytes *= dim;
    result.push_back({std::move(name), std::move(shape), bytes});
  };
  const auto norm = [&](const std::string& name) {
    add(name + ".weight", {256});
    add(name + ".bias", {256});
  };
  const auto linear = [&](const std::string& name, int in, int out) {
    add(name + ".weight", {out, in});
    add(name + ".bias", {out});
  };
  add("encoder.conv1d.weight", {256, 1, 16});
  add("decoder.weight", {256, 1, 16});
  norm("masknet.norm");
  add("masknet.conv1d.weight", {256, 256, 1});
  for (int block = 0; block < SeparatorConfig::kBlocks; ++block) {
    const auto base = "masknet.dual_mdl." + std::to_string(block);
    for (const auto* path : {"intra", "inter"}) {
      const auto model = base + "." + path + "_mdl";
      for (int layer = 0; layer < SeparatorConfig::kLayers; ++layer) {
        const auto name = model + ".mdl.layers." + std::to_string(layer);
        norm(name + ".norm1.norm");
        norm(name + ".norm2.norm");
        add(name + ".self_att.att.in_proj_weight", {768, 256});
        add(name + ".self_att.att.in_proj_bias", {768});
        linear(name + ".self_att.att.out_proj", 256, 256);
        linear(name + ".pos_ffn.ffn.0", 256, 1024);
        linear(name + ".pos_ffn.ffn.3", 1024, 256);
      }
      norm(model + ".mdl.norm.norm");
      add(model + ".pos_enc.pe", {1, 2500, 256});
      norm(base + "." + path + "_norm");
    }
  }
  add("masknet.prelu.weight", {1});
  add("masknet.conv2d.weight", {512, 256, 1, 1});
  add("masknet.conv2d.bias", {512});
  for (const auto* name : {"masknet.output.0", "masknet.output_gate.0"}) {
    add(std::string(name) + ".weight", {256, 256, 1});
    add(std::string(name) + ".bias", {256});
  }
  add("masknet.end_conv1x1.weight", {256, 256, 1});
  std::sort(result.begin(), result.end(), [](const auto& a, const auto& b) {
    return a.name < b.name;
  });
  return result;
}

Status ValidateSeparatorConfig(const io::Json& config) {
  if (!Fields(config, {"profile", "model_id", "revision", "source_revision",
                       "weight_sha256", "sample_rate", "sources", "encoder",
                       "separator", "decoder"}) ||
      !Text(config, "profile", SeparatorConfig::kProfile) ||
      !Text(config, "model_id", SeparatorConfig::kModelId) ||
      !Text(config, "revision", SeparatorConfig::kRevision) ||
      !Text(config, "source_revision", SeparatorConfig::kSourceRevision) ||
      !Text(config, "weight_sha256", SeparatorConfig::kWeightSha256) ||
      !Number(config.Find("sample_rate"), 8000) ||
      !Number(config.Find("sources"), 2))
    return Status::Fail("Separator fixed identity differs");
  const auto& encoder = *config.Find("encoder");
  if (!Fields(encoder, {"channels", "kernel", "stride", "padding",
                        "activation"}) ||
      !Number(encoder.Find("channels"), 256) ||
      !Number(encoder.Find("kernel"), 16) ||
      !Number(encoder.Find("stride"), 8) ||
      !Number(encoder.Find("padding"), 0) ||
      !Text(encoder, "activation", "relu"))
    return Status::Fail("Separator trained encoder differs");
  const auto& model = *config.Find("separator");
  if (!Fields(model, {"chunk", "dual_blocks", "layers_per_path", "heads",
                      "ffn", "group_norm_epsilon", "layer_norm_epsilon",
                      "activation", "pre_norm", "position_frames",
                      "overlap_add", "skip_intra", "linear_after_path"}) ||
      !Number(model.Find("chunk"), 250) ||
      !Number(model.Find("dual_blocks"), 2) ||
      !Number(model.Find("layers_per_path"), 8) ||
      !Number(model.Find("heads"), 8) ||
      !Number(model.Find("ffn"), 1024) ||
      !Number(model.Find("group_norm_epsilon"), 1e-8) ||
      !Number(model.Find("layer_norm_epsilon"), 1e-6) ||
      !Number(model.Find("position_frames"), 2500) ||
      !Text(model, "activation", "relu") ||
      !Text(model, "overlap_add", "sum") ||
      !Flag(model, "pre_norm", true) || !Flag(model, "skip_intra", true) ||
      !Flag(model, "linear_after_path", false))
    return Status::Fail("Separator trained mask graph differs");
  const auto& decoder = *config.Find("decoder");
  if (!Fields(decoder, {"channels", "kernel", "stride", "padding", "bias",
                        "tail"}) ||
      !Number(decoder.Find("channels"), 256) ||
      !Number(decoder.Find("kernel"), 16) ||
      !Number(decoder.Find("stride"), 8) ||
      !Number(decoder.Find("padding"), 0) || !Flag(decoder, "bias", false) ||
      !Text(decoder, "tail", "zero_to_input_length"))
    return Status::Fail("Separator trained decoder differs");
  return {};
}

Status ParseSeparatorTensorLayout(const io::Json& header, uint64_t data_bytes,
                                  SeparatorTensorLayout* out,
                                  std::function<bool()> cancelled) {
  const auto interrupted = [&] { return cancelled && cancelled(); };
  if (interrupted()) return Status::Fail("Separator layout cancelled");
  if (!out || !header.IsObject() ||
      header.object.size() != SeparatorConfig::kTensors ||
      data_bytes != SeparatorConfig::kWeightBytes)
    return Status::Fail("Separator tensor count or bytes differ");
  std::set<std::string_view> names;
  for (const auto& [name, unused] : header.object) {
    (void)unused;
    if (!names.insert(name).second)
      return Status::Fail("Separator tensor name duplicated");
  }
  const auto contracts = SeparatorTensorContracts();
  SeparatorTensorLayout layout;
  size_t offset = 0;
  for (size_t i = 0; i < contracts.size(); ++i) {
    if (interrupted()) return Status::Fail("Separator layout cancelled");
    const auto& spec = contracts[i];
    const auto* value = header.Find(spec.name);
    if (!value || !Fields(*value, {"dtype", "shape", "data_offsets"}) ||
        !Text(*value, "dtype", "F32"))
      return Status::Fail("Separator tensor name or dtype differs");
    const auto* shape = value->GetArray("shape");
    const auto* bounds = value->GetArray("data_offsets");
    if (!shape || shape->array.size() != spec.shape.size() || !bounds ||
        bounds->array.size() != 2)
      return Status::Fail("Separator tensor rank or intervals differ");
    for (size_t j = 0; j < spec.shape.size(); ++j)
      if (!Number(&shape->array[j], spec.shape[j]))
        return Status::Fail("Separator tensor dimension differs");
    if (!Number(&bounds->array[0], offset) ||
        !Number(&bounds->array[1], offset + spec.bytes))
      return Status::Fail("Separator tensor storage is not canonical");
    layout.offsets[i] = offset;
    offset += spec.bytes;
  }
  if (offset != data_bytes || interrupted())
    return Status::Fail("Separator tensor map incomplete or cancelled");
  *out = layout;
  return {};
}
}  // namespace q4t::audio
