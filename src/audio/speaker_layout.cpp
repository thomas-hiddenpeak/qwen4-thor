#include "q4t/audio/speaker_layout.h"

#include <algorithm>
#include <limits>
#include <map>

namespace q4t::audio {
Status BindSpeakerDeviceWeights(const void* data, size_t bytes,
                                const SpeakerTensorLayout& layout,
                                SpeakerDeviceWeights* out) {
  const auto base = reinterpret_cast<uintptr_t>(data);
  if (!data || !out || base % alignof(float) ||
      bytes != SpeakerConfig::kWeightBytes ||
      base > std::numeric_limits<uintptr_t>::max() - bytes)
    return Status::Fail("Speaker device payload bounds differ");
  const auto contracts = SpeakerTensorContracts();
  std::map<std::string, const float*> values;
  size_t offset = 0;
  for (size_t i = 0; i < contracts.size(); ++i) {
    if (layout.offsets[i] != offset)
      return Status::Fail("Speaker weight layout differs");
    if (contracts[i].dtype == "F32")
      values.emplace(contracts[i].name,
                     reinterpret_cast<const float*>(base + offset));
    offset += contracts[i].bytes;
  }
  if (offset != bytes) return Status::Fail("Speaker weight size differs");
  SpeakerDeviceWeights weights;
  const auto affine = [&](const std::string& name) {
    return SpeakerAffineWeights{values.at(name + ".conv.weight"),
                                values.at(name + ".conv.bias")};
  };
  const auto norm = [&](const std::string& name) {
    return SpeakerNormWeights{values.at(name + ".norm.weight"),
                              values.at(name + ".norm.bias"),
                              values.at(name + ".norm.running_mean"),
                              values.at(name + ".norm.running_var")};
  };
  const auto tdnn = [&](const std::string& name) {
    return SpeakerTdnnWeights{affine(name + ".conv"), norm(name + ".norm")};
  };
  weights.input = tdnn("blocks.0");
  for (size_t i = 0; i < weights.layers.size(); ++i) {
    const auto name = "blocks." + std::to_string(i + 1);
    auto& layer = weights.layers[i];
    layer.before = tdnn(name + ".tdnn1");
    layer.after = tdnn(name + ".tdnn2");
    for (size_t j = 0; j < layer.branches.size(); ++j)
      layer.branches[j] =
          tdnn(name + ".res2net_block.blocks." + std::to_string(j));
    layer.se_first = affine(name + ".se_block.conv1");
    layer.se_second = affine(name + ".se_block.conv2");
  }
  weights.aggregate = tdnn("mfa");
  weights.attention = tdnn("asp.tdnn");
  weights.attention_output = affine("asp.conv");
  weights.pool_norm = norm("asp_bn");
  weights.output = affine("fc");
  *out = weights;
  return {};
}

Status PlanSpeakerWorkspace(size_t frames, size_t max_frames, size_t max_bytes,
                            SpeakerWorkspaceLayout* out) {
  // Reflect padding of the largest dilated convolution needs >4 frames.
  if (!out || frames < 5 || frames > max_frames ||
      frames > SpeakerWorkspaceLayout::kMaxFrames)
    return Status::Fail("Speaker frame count or budget invalid");
  SpeakerWorkspaceLayout layout;
  layout.frames = static_cast<int>(frames);
  const auto carve = [&](size_t bytes) {
    const size_t begin = (layout.bytes + 255) / 256 * 256;
    layout.bytes = begin + bytes;
    return begin;
  };
  // All products are bounded by 3001 frames and fit a 32-bit size_t.
  const auto matrix = [&](size_t channels) {
    return carve(channels * frames * sizeof(float));
  };
  layout.mel = matrix(80);
  layout.hidden = matrix(1024);
  layout.temporary = matrix(1024);
  layout.res2 = matrix(1024);
  layout.update = matrix(1024);
  layout.history = matrix(3072);
  layout.aggregate = matrix(3072);
  layout.context = matrix(9216);
  layout.attention_hidden = matrix(128);
  layout.attention = matrix(3072);
  layout.columns = matrix(400);
  layout.pool = carve(6144 * sizeof(float));
  layout.squeeze = carve(1024 * sizeof(float));
  layout.squeeze_hidden = carve(128 * sizeof(float));
  layout.squeeze_gate = carve(1024 * sizeof(float));
  layout.output = carve(192 * sizeof(float));
  layout.gemm = carve(SpeakerWorkspaceLayout::kGemmBytes);
  layout.finite_flag = carve(sizeof(int));
  if (layout.bytes > max_bytes)
    return Status::Fail("Speaker workspace exceeds byte budget");
  *out = layout;
  return {};
}
}  // namespace q4t::audio
