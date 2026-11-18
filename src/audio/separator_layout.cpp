#include "q4t/audio/separator_layout.h"

#include <algorithm>
#include <limits>
#include <map>

namespace q4t::audio {
Status BindSeparatorDeviceWeights(const void* data, size_t bytes,
                                  const SeparatorTensorLayout& layout,
                                  SeparatorDeviceWeights* out) {
  const auto base = reinterpret_cast<uintptr_t>(data);
  if (!data || !out || base % alignof(float) ||
      bytes != SeparatorConfig::kWeightBytes ||
      base > std::numeric_limits<uintptr_t>::max() - bytes)
    return Status::Fail("Separator device payload bounds differ");
  const auto specs = SeparatorTensorContracts();
  std::map<std::string, const float*> values;
  size_t offset = 0;
  for (size_t i = 0; i < specs.size(); ++i) {
    if (layout.offsets[i] != offset)
      return Status::Fail("Separator weight layout differs");
    values.emplace(specs[i].name,
                   reinterpret_cast<const float*>(base + offset));
    offset += specs[i].bytes;
  }
  if (offset != bytes) return Status::Fail("Separator weight size differs");
  const auto affine = [&](const std::string& name) {
    return SeparatorAffineWeights{values.at(name + ".weight"),
                                  values.at(name + ".bias")};
  };
  SeparatorDeviceWeights weights;
  weights.encoder = values.at("encoder.conv1d.weight");
  weights.decoder = values.at("decoder.weight");
  weights.norm = affine("masknet.norm");
  weights.project = values.at("masknet.conv1d.weight");
  weights.prelu = values.at("masknet.prelu.weight");
  weights.sources = affine("masknet.conv2d");
  weights.output = affine("masknet.output.0");
  weights.gate = affine("masknet.output_gate.0");
  weights.mask_project = values.at("masknet.end_conv1x1.weight");
  for (size_t p = 0; p < weights.paths.size(); ++p) {
    auto& path = weights.paths[p];
    const auto base = "masknet.dual_mdl." + std::to_string(p / 2);
    const auto kind = p % 2 == 0 ? "intra" : "inter";
    const auto name = base + "." + kind + "_mdl";
    path.position = values.at(name + ".pos_enc.pe");
    path.norm = affine(name + ".mdl.norm.norm");
    path.group_norm = affine(base + "." + kind + "_norm");
    for (size_t i = 0; i < path.layers.size(); ++i) {
      auto& layer = path.layers[i];
      const auto at = name + ".mdl.layers." + std::to_string(i);
      layer.norm1 = affine(at + ".norm1.norm");
      layer.norm2 = affine(at + ".norm2.norm");
      layer.qkv = {values.at(at + ".self_att.att.in_proj_weight"),
                   values.at(at + ".self_att.att.in_proj_bias")};
      layer.attention = affine(at + ".self_att.att.out_proj");
      layer.ffn1 = affine(at + ".pos_ffn.ffn.0");
      layer.ffn2 = affine(at + ".pos_ffn.ffn.3");
    }
  }
  *out = weights;
  return {};
}

Status PlanSeparatorWorkspace(size_t samples, size_t max_samples,
                              size_t max_bytes, SeparatorWorkspaceLayout* out) {
  if (!out || samples < 16 || samples > max_samples ||
      samples > SeparatorWorkspaceLayout::kMaxSamples)
    return Status::Fail("Separator sample count or budget invalid");
  SeparatorWorkspaceLayout layout;
  layout.samples = static_cast<int>(samples);
  layout.frames = (layout.samples - 16) / 8 + 1;
  layout.decoded_samples = (layout.frames - 1) * 8 + 16;
  layout.gap = 250 - (125 + layout.frames % 250) % 250;
  layout.segments = (layout.frames + layout.gap + 125) / 125;
  layout.rows = 250 * layout.segments;
  layout.reduction_blocks = (layout.rows * 256 + 1023) / 1024;
  const auto carve = [&](size_t bytes) {
    const size_t begin = (layout.bytes + 255) / 256 * 256;
    layout.bytes = begin + bytes;
    return begin;
  };
  const auto floats = [&](size_t count) {
    return carve(count * sizeof(float));
  };
  // All products are bounded by the fixed 240000-sample limit; no untrusted
  // dimensions reach allocation arithmetic. Matrices use row-major channels.
  const size_t frames = layout.frames, rows = layout.rows;
  layout.pcm = floats(samples);
  layout.encoded = floats(frames * 256);
  layout.normalized = floats(frames * 256);
  layout.projected = floats(frames * 256);
  layout.hidden = floats(rows * 256);
  layout.path = floats(rows * 256);
  layout.norm = floats(rows * 256);
  layout.qkv = floats(rows * 768);
  layout.scores = floats(8 * rows * std::max(250, layout.segments));
  layout.attention = floats(rows * 256);
  layout.ffn = floats(rows * 1024);
  layout.update = floats(rows * 256);
  layout.source_chunks = floats(rows * 512);
  layout.joined = floats(2 * frames * 256);
  layout.output_hidden = floats(2 * frames * 256);
  layout.gate = floats(2 * frames * 256);
  layout.masks = floats(2 * frames * 256);
  layout.separated = floats(2 * samples);
  layout.partial = floats(layout.reduction_blocks);
  layout.statistics = floats(2);
  layout.gemm = carve(SeparatorWorkspaceLayout::kGemmBytes);
  layout.finite_flag = carve(sizeof(int));
  if (layout.bytes > max_bytes)
    return Status::Fail("Separator workspace exceeds byte budget");
  *out = layout;
  return {};
}
}  // namespace q4t::audio
