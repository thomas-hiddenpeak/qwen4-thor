#include "q4t/audio/tts_codec_layout.h"

#include <algorithm>
#include <limits>
#include <map>

namespace q4t::audio {

Status BindTtsCodecDeviceWeights(const void* data, size_t bytes,
                                 const TtsWeightLayout& layout,
                                 TtsCodecDeviceWeights* out) {
  const auto base = reinterpret_cast<uintptr_t>(data);
  if (!base || !out || base % alignof(float) || bytes != layout.bytes ||
      base > std::numeric_limits<uintptr_t>::max() - bytes)
    return Status::Fail("TTS codec device payload invalid");
  if (auto s = ValidateTtsWeightLayout(
          layout, TtsVariant::kCustomVoice17B, true); !s)
    return s;
  std::map<std::string, const float*> pointers;
  for (const auto& t : layout.tensors)
    pointers.emplace(t.name, reinterpret_cast<const float*>(base + t.offset));
  const auto weight = [&](const std::string& name) {
    return pointers.at("decoder." + name);
  };
  const auto affine = [&](const std::string& name) {
    return TtsAffineWeights{weight(name + ".weight"), weight(name + ".bias")};
  };
  const auto snake = [&](const std::string& name) {
    return TtsCodecSnakeWeights{weight(name + ".alpha"), weight(name + ".beta")};
  };
  const auto conv = [&](const std::string& name, int input, int output,
                         int kernel, int stride = 1, int dilation = 1,
                         int groups = 1, bool transposed = false) {
    return TtsCodecConvWeights{weight(name + ".weight"), weight(name + ".bias"),
        input, output, kernel, stride, dilation, groups, transposed};
  };
  TtsCodecDeviceWeights w;
  for (int group = 0; group < 16; ++group) {
    const std::string set = group == 0 ? "rvq_first" : "rvq_rest";
    const auto name = "quantizer." + set + ".vq.layers." +
        std::to_string(group == 0 ? 0 : group - 1) + "._codebook.";
    w.usage[group] = weight(name + "cluster_usage");
    w.embedding_sum[group] = weight(name + "embedding_sum");
  }
  w.rvq_projection[0] = weight("quantizer.rvq_first.output_proj.weight");
  w.rvq_projection[1] = weight("quantizer.rvq_rest.output_proj.weight");
  w.pre_conv = conv("pre_conv.conv", 512, 1024, 3);
  w.input_projection = affine("pre_transformer.input_proj");
  w.output_projection = affine("pre_transformer.output_proj");
  w.final_norm = weight("pre_transformer.norm.weight");
  for (int i = 0; i < 8; ++i) {
    auto& layer = w.layers[i];
    const auto name = "pre_transformer.layers." + std::to_string(i) + ".";
    layer.input_norm = weight(name + "input_layernorm.weight");
    layer.post_norm = weight(name + "post_attention_layernorm.weight");
    layer.q = weight(name + "self_attn.q_proj.weight");
    layer.k = weight(name + "self_attn.k_proj.weight");
    layer.v = weight(name + "self_attn.v_proj.weight");
    layer.o = weight(name + "self_attn.o_proj.weight");
    layer.gate = weight(name + "mlp.gate_proj.weight");
    layer.up = weight(name + "mlp.up_proj.weight");
    layer.down = weight(name + "mlp.down_proj.weight");
    layer.attention_scale = weight(name + "self_attn_layer_scale.scale");
    layer.mlp_scale = weight(name + "mlp_layer_scale.scale");
  }
  for (int i = 0; i < 2; ++i) {
    auto& up = w.upsample[i];
    const auto name = "upsample." + std::to_string(i) + ".";
    up.transpose = conv(name + "0.conv", 1024, 1024, 2, 2, 1, 1, true);
    up.depthwise = conv(name + "1.dwconv.conv", 1024, 1024, 7, 1, 1, 1024);
    up.norm = affine(name + "1.norm");
    up.fc1 = affine(name + "1.pwconv1");
    up.fc2 = affine(name + "1.pwconv2");
    up.gamma = weight(name + "1.gamma");
  }
  w.vocoder_input = conv("decoder.0.conv", 1024, 1536, 7);
  constexpr int strides[] = {8, 5, 4, 3};
  constexpr int dilations[] = {1, 3, 9};
  for (int i = 0; i < 4; ++i) {
    auto& stage = w.vocoder[i];
    const auto name = "decoder." + std::to_string(i + 1) + ".block.";
    const int input = 1536 >> i, output = input / 2;
    stage.act = snake(name + "0");
    stage.transpose = conv(name + "1.conv", input, output,
                            strides[i] * 2, strides[i], 1, 1, true);
    for (int j = 0; j < 3; ++j) {
      auto& residual = stage.residuals[j];
      const auto unit = name + std::to_string(j + 2) + ".";
      residual.act1 = snake(unit + "act1");
      residual.act2 = snake(unit + "act2");
      residual.conv1 = conv(unit + "conv1.conv", output, output, 7,
                             1, dilations[j]);
      residual.conv2 = conv(unit + "conv2.conv", output, output, 1);
    }
  }
  w.final_act = snake("decoder.5");
  w.vocoder_output = conv("decoder.6.conv", 96, 1, 7);
  *out = w;
  return {};
}

Status PlanTtsCodecWorkspace(size_t frames, size_t max_bytes,
                             TtsCodecLayout* out) {
  if (!out || frames == 0 || frames > TtsCodecConfig::kMaxInputFrames)
    return Status::Fail("TTS codec frame budget exceeded");
  TtsCodecLayout w;
  w.frames = frames;
  w.padded_frames = (frames + 15) / 16 * 16;
  w.samples = frames * TtsConfig::kSamplesPerFrame;
  w.buffer_elements = w.samples * 96;
  const auto carve = [&](size_t bytes) {
    const size_t begin = (w.bytes + 255) / 256 * 256;
    w.bytes = begin + bytes;
    return begin;
  };
  const auto fp32 = [&](size_t elements) { return carve(elements * sizeof(float)); };
  for (auto& buffer : w.wave_buffers) buffer = fp32(w.buffer_elements);
  w.codes = carve(frames * sizeof(TtsCodes));
  w.rvq_first = fp32(frames * 256);
  w.rvq_rest = fp32(frames * 256);
  w.hidden = fp32(frames * 512);
  w.normalized = fp32(frames * 512);
  w.projected = fp32(frames * 512);
  w.q = fp32(frames * 1024);
  w.k = fp32(frames * 1024);
  w.v = fp32(frames * 1024);
  w.head_q = fp32(w.padded_frames * 1024);
  w.head_k = fp32(w.padded_frames * 1024);
  w.head_v = fp32(w.padded_frames * 1024);
  w.rope = fp32(frames * 64);
  w.scores = fp32(frames * w.padded_frames);
  w.head_output = fp32(frames * 64);
  w.attention = fp32(frames * 1024);
  w.gate = fp32(frames * 1024);
  w.up = fp32(frames * 1024);
  w.columns = carve(TtsCodecConfig::kColumnBytes);
  w.transpose_weights = carve(TtsCodecConfig::kTransposeWeightBytes);
  w.gemm = carve(TtsDenseLayout::kGemmBytes);
  if (w.bytes > max_bytes)
    return Status::Fail("TTS codec unified RAM budget exceeded");
  *out = w;
  return {};
}

Status ValidateTtsCodecCodes(std::span<const TtsCodes> codes) {
  if (codes.empty() || codes.size() > TtsCodecConfig::kMaxInputFrames)
    return Status::Fail("TTS codec code-frame count invalid");
  for (const auto& frame : codes)
    for (const auto code : frame)
      if (code < 0 || code >= TtsConfig::kCodebook)
        return Status::Fail("TTS codec input contains a non-audio code");
  return {};
}

Status PlanTtsCodecChunks(size_t frames, size_t chunk_frames,
                          size_t max_output_samples,
                          std::vector<TtsCodecChunk>* out) {
  if (!out || frames == 0 || frames > 4096 || chunk_frames == 0 ||
      chunk_frames > TtsCodecConfig::kChunkFrames ||
      frames * TtsConfig::kSamplesPerFrame > max_output_samples)
    return Status::Fail("TTS codec output or chunk budget exceeded");
  std::vector<TtsCodecChunk> chunks;
  for (size_t begin = 0; begin < frames; begin += chunk_frames) {
    const size_t context = std::min(begin, TtsCodecConfig::kLeftContext);
    const size_t count = std::min(chunk_frames, frames - begin);
    chunks.push_back({begin - context, context + count,
        context * TtsConfig::kSamplesPerFrame,
        count * TtsConfig::kSamplesPerFrame});
  }
  *out = std::move(chunks);
  return {};
}

}  // namespace q4t::audio
