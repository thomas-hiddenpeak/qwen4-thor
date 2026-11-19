#include "q4t/audio/tts_encoder_layout.h"

#include <algorithm>
#include <cmath>
#include <limits>
#include <map>

namespace q4t::audio {

std::vector<TtsTensorContract> TtsEncoderTensorContracts() {
  std::vector<TtsTensorContract> tensors;
  const auto add = [&](std::string name, std::vector<int64_t> shape) {
    size_t bytes = sizeof(float);
    for (const auto dimension : shape) bytes *= dimension;
    tensors.push_back({"encoder." + name, std::move(shape), bytes});
  };
  const auto conv = [&](const std::string& name, int input, int output,
                         int kernel, bool bias = true) {
    add(name + ".weight", {output, input, kernel});
    if (bias) add(name + ".bias", {output});
  };
  conv("encoder.layers.0.conv", 1, 64, 7);
  constexpr int ratios[] = {4, 5, 6, 8};
  for (int i = 0; i < 4; ++i) {
    const int width = 64 << i;
    const auto block = "encoder.layers." + std::to_string(1 + i * 3);
    conv(block + ".block.1.conv", width, width / 2, 3);
    conv(block + ".block.3.conv", width / 2, width, 1);
    conv("encoder.layers." + std::to_string(3 + i * 3) + ".conv",
         width, width * 2, ratios[i] * 2);
  }
  conv("encoder.layers.14.conv", 1024, 512, 3);
  for (int i = 0; i < 8; ++i) {
    const auto p = "encoder_transformer.layers." + std::to_string(i) + ".";
    for (const auto* norm : {"input_layernorm", "post_attention_layernorm"}) {
      add(p + norm + ".weight", {512});
      add(p + norm + ".bias", {512});
    }
    for (const auto* name : {"q_proj", "k_proj", "v_proj", "o_proj"})
      add(p + "self_attn." + name + ".weight", {512, 512});
    add(p + "mlp.fc1.weight", {2048, 512});
    add(p + "mlp.fc2.weight", {512, 2048});
    add(p + "self_attn_layer_scale.scale", {512});
    add(p + "mlp_layer_scale.scale", {512});
  }
  conv("downsample.conv", 512, 512, 4, false);
  for (const auto* name : {"semantic", "acoustic"}) {
    const auto p = std::string("quantizer.") + name +
                   "_residual_vector_quantizer.";
    conv(p + "input_proj", 512, 256, 1, false);
    conv(p + "output_proj", 256, 512, 1, false);
    const int count = std::string_view(name) == "semantic" ? 1 : 31;
    for (int i = 0; i < count; ++i) {
      const auto c = p + "layers." + std::to_string(i) + ".codebook.";
      add(c + "cluster_usage", {2048});
      add(c + "embed_sum", {2048, 256});
      add(c + "initialized", {1});
    }
  }
  std::sort(tensors.begin(), tensors.end(),
             [](const auto& a, const auto& b) { return a.name < b.name; });
  return tensors;
}

Status ValidateTtsEncoderTensorHeader(const io::Json& header,
                                      uint64_t data_bytes) {
  // Existing validation certifies every extent and all decoder shapes. Add
  // exact encoder names/shapes rather than accepting arbitrary encoder.* data.
  if (auto status = ValidateTtsCodecTensorHeader(header, data_bytes); !status)
    return status;
  for (const auto& spec : TtsEncoderTensorContracts()) {
    const auto* tensor = header.Find(spec.name);
    const auto* shape = tensor ? tensor->GetArray("shape") : nullptr;
    if (!shape || shape->array.size() != spec.shape.size())
      return Status::Fail("TTS encoder tensor missing or rank differs");
    for (size_t i = 0; i < spec.shape.size(); ++i)
      if (!shape->array[i].IsNumber() ||
          shape->array[i].number != spec.shape[i])
        return Status::Fail("TTS encoder tensor dimension differs");
  }
  return {};
}

Status ParseTtsEncoderWeightLayout(const io::Json& header, size_t data_bytes,
                                   TtsWeightLayout* out) {
  if (!out) return Status::Fail("TTS encoder layout owner is null");
  if (auto s = ValidateTtsEncoderTensorHeader(header, data_bytes); !s) return s;
  TtsWeightLayout result;
  result.file_bytes = data_bytes;
  for (const auto& spec : TtsEncoderTensorContracts()) {
    const auto offset =
        header.Find(spec.name)->GetArray("data_offsets")->array[0].AsInt(-1);
    result.tensors.push_back(
        {spec.name, static_cast<size_t>(offset), result.bytes, spec.bytes});
    result.bytes += spec.bytes;
  }
  if (auto s = ValidateTtsEncoderWeightLayout(result); !s) return s;
  *out = std::move(result);
  return {};
}

Status ValidateTtsEncoderWeightLayout(const TtsWeightLayout& layout) {
  const auto specs = TtsEncoderTensorContracts();
  if (layout.file_bytes != TtsConfig::kCodecFileDataBytes ||
      layout.bytes != TtsEncoderConfig::kWeightBytes ||
      layout.tensors.size() != specs.size() || specs.size() != 225)
    return Status::Fail("TTS encoder weight storage differs");
  size_t offset = 0;
  std::vector<std::pair<size_t, size_t>> ranges;
  for (size_t i = 0; i < specs.size(); ++i) {
    const auto& t = layout.tensors[i];
    if (t.name != specs[i].name || t.bytes != specs[i].bytes ||
        t.offset != offset || t.file_offset % sizeof(float) ||
        t.file_offset < TtsConfig::kCodecDecoderBytes ||
        t.file_offset > layout.file_bytes ||
        t.bytes > layout.file_bytes - t.file_offset)
      return Status::Fail("TTS encoder tensor extent differs");
    ranges.emplace_back(t.file_offset, t.file_offset + t.bytes);
    offset += t.bytes;
  }
  std::sort(ranges.begin(), ranges.end());
  size_t end = TtsConfig::kCodecDecoderBytes;
  for (const auto& [begin, next] : ranges) {
    if (begin != end)
      return Status::Fail("TTS encoder source overlap or gap");
    end = next;
  }
  if (end != layout.file_bytes || offset != layout.bytes)
    return Status::Fail("TTS encoder graph size differs");
  return {};
}

Status BindTtsEncoderDeviceWeights(const void* data, size_t bytes,
                                   const TtsWeightLayout& layout,
                                   TtsEncoderDeviceWeights* out) {
  const auto base = reinterpret_cast<uintptr_t>(data);
  if (!base || !out || base % alignof(float) ||
      bytes != TtsEncoderConfig::kWeightBytes ||
      base > std::numeric_limits<uintptr_t>::max() - bytes)
    return Status::Fail("TTS encoder device storage invalid");
  if (auto s = ValidateTtsEncoderWeightLayout(layout); !s) return s;
  std::map<std::string, const float*> pointers;
  for (const auto& t : layout.tensors)
    pointers.emplace(t.name, reinterpret_cast<const float*>(base + t.offset));
  const auto weight = [&](const std::string& name) {
    return pointers.at("encoder." + name);
  };
  const auto affine = [&](const std::string& name) {
    return TtsAffineWeights{weight(name + ".weight"), weight(name + ".bias")};
  };
  const auto conv = [&](const std::string& name, int input, int output,
                         int kernel, int stride = 1, bool bias = true) {
    return TtsCodecConvWeights{weight(name + ".weight"),
        bias ? weight(name + ".bias") : nullptr,
        input, output, kernel, stride};
  };
  TtsEncoderDeviceWeights w;
  w.input = conv("encoder.layers.0.conv", 1, 64, 7);
  constexpr int ratios[] = {4, 5, 6, 8};
  for (int i = 0; i < 4; ++i) {
    const int width = 64 << i;
    const auto p = "encoder.layers." + std::to_string(1 + i * 3);
    w.blocks[i].first = conv(p + ".block.1.conv", width, width / 2, 3);
    w.blocks[i].second = conv(p + ".block.3.conv", width / 2, width, 1);
    w.blocks[i].downsample = conv(
        "encoder.layers." + std::to_string(3 + i * 3) + ".conv",
        width, width * 2, 2 * ratios[i], ratios[i]);
  }
  w.output = conv("encoder.layers.14.conv", 1024, 512, 3);
  w.downsample = conv("downsample.conv", 512, 512, 4, 2, false);
  for (int i = 0; i < 8; ++i) {
    const auto p = "encoder_transformer.layers." + std::to_string(i) + ".";
    auto& layer = w.layers[i];
    layer.input_norm = affine(p + "input_layernorm");
    layer.post_norm = affine(p + "post_attention_layernorm");
    layer.q = weight(p + "self_attn.q_proj.weight");
    layer.k = weight(p + "self_attn.k_proj.weight");
    layer.v = weight(p + "self_attn.v_proj.weight");
    layer.o = weight(p + "self_attn.o_proj.weight");
    layer.fc1 = weight(p + "mlp.fc1.weight");
    layer.fc2 = weight(p + "mlp.fc2.weight");
    layer.attention_scale = weight(p + "self_attn_layer_scale.scale");
    layer.mlp_scale = weight(p + "mlp_layer_scale.scale");
  }
  for (int group = 0; group < 16; ++group) {
    const auto prefix = std::string("quantizer.") +
        (group == 0 ? "semantic" : "acoustic") +
        "_residual_vector_quantizer.";
    const auto p = prefix + "layers." +
        std::to_string(group == 0 ? 0 : group - 1) + ".codebook.";
    w.usage[group] = weight(p + "cluster_usage");
    w.embedding_sum[group] = weight(p + "embed_sum");
    w.initialized[group] = weight(p + "initialized");
    if (group < 2)
      w.quantizer_projection[group] = weight(prefix + "input_proj.weight");
  }
  *out = w;
  return {};
}

Status PlanTtsEncoderConvolution(const TtsCodecConvWeights& w, size_t rows,
                                 size_t* output_rows, size_t* left_padding) {
  if (!output_rows || !left_padding || rows == 0 ||
      rows > TtsEncoderConfig::kMaxSamples || w.input < 1 || w.input > 1024 ||
      w.output < 1 || w.output > 1024 || w.stride < 1 || w.stride > 8 ||
      w.kernel < w.stride || w.kernel > 16 || w.dilation != 1 ||
      w.groups != 1 || w.transposed)
    return Status::Fail("TTS encoder convolution shape invalid");
  *output_rows = (rows + w.stride - 1) / w.stride;
  *left_padding = w.kernel - w.stride;
  return {};
}

Status PlanTtsEncoderWorkspace(size_t samples, size_t max_bytes,
                               TtsEncoderWorkspace* out) {
  if (!out || !samples || samples > TtsEncoderConfig::kMaxSamples)
    return Status::Fail("TTS encoder sample budget exceeded");
  TtsEncoderWorkspace w;
  w.samples = samples;
  w.transformer_rows = (samples + 959) / 960;
  w.padded_rows = (w.transformer_rows + 15) / 16 * 16;
  w.frames = (samples + 1919) / 1920;
  // Tiny inputs still visit a 1024-channel final SEANet layer.
  w.wave_elements = std::max(samples * 64, size_t{1024});
  const auto carve = [&](size_t bytes) {
    const size_t start = (w.bytes + 255) / 256 * 256;
    w.bytes = start + bytes;
    return start;
  };
  const auto fp32 = [&](size_t elements) { return carve(elements * 4); };
  for (auto& wave : w.wave) wave = fp32(w.wave_elements);
  const auto hidden = [&] { return fp32(w.transformer_rows * 512); };
  w.hidden = hidden();
  w.normalized = hidden();
  w.projected = hidden();
  w.q = hidden();
  w.k = hidden();
  w.v = hidden();
  w.head_q = fp32(w.padded_rows * 512);
  w.head_k = fp32(w.padded_rows * 512);
  w.head_v = fp32(w.padded_rows * 512);
  w.rope = fp32(w.transformer_rows * 64);
  w.scores = fp32(w.transformer_rows * w.padded_rows);
  w.head_output = fp32(w.transformer_rows * 64);
  w.attention = hidden();
  w.feedforward = fp32(w.transformer_rows * 2048);
  w.residual = fp32(w.frames * 256);
  w.codebook = fp32(2048 * 256);
  w.distances = fp32(w.frames * 2048);
  w.codes = carve(w.frames * sizeof(TtsCodes));
  w.columns = carve(TtsCodecConfig::kColumnBytes);
  w.gemm = carve(TtsDenseLayout::kGemmBytes);
  w.finite_flag = carve(sizeof(int32_t));
  if (w.bytes > max_bytes)
    return Status::Fail("TTS encoder unified RAM budget exceeded");
  *out = w;
  return {};
}

Status ValidateTtsEncoderPcm(std::span<const float> samples, int sample_rate) {
  if (sample_rate != 24000 || samples.empty() ||
      samples.size() > TtsEncoderConfig::kMaxSamples)
    return Status::Fail("TTS encoder requires bounded mono 24 kHz PCM");
  for (const auto sample : samples)
    if (!std::isfinite(sample) || sample < -1 || sample > 1)
      return Status::Fail("TTS encoder PCM must be finite and normalized");
  return {};
}

}  // namespace q4t::audio
