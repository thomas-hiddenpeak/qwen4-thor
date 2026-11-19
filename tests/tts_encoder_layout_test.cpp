#include "q4t/audio/tts_encoder_layout.h"
#include "q4t/test.h"

#include <algorithm>
#include <filesystem>
#include <fstream>
#include <limits>
#include <numeric>
#include <sstream>

namespace {
using namespace q4t::audio;
using q4t::io::Json;
constexpr size_t kUnlimited = std::numeric_limits<size_t>::max();

Json Fixture(const char* name = "codec-header.json") {
  std::ifstream file(std::filesystem::path(__FILE__).parent_path() /
                     "fixtures/tts" / name);
  std::ostringstream text;
  text << file.rdbuf();
  Json value;
  if (!file || !q4t::io::ParseJson(text.str(), &value, false,
                                   {.reject_duplicate_keys = true}))
    throw std::runtime_error("encoder fixture missing");
  return value;
}

Json& Field(Json& value, const std::string& name) {
  for (auto& [key, item] : value.object)
    if (key == name) return item;
  throw std::runtime_error("encoder fixture field missing");
}
}  // namespace

Q4T_TEST(tts_encoder_graph_matches_full_official_checkpoint) {
  const auto tensors = TtsEncoderTensorContracts();
  Q4T_CHECK(tensors.size() == 225);
  const auto bytes = std::accumulate(
      tensors.begin(), tensors.end(), size_t{},
      [](size_t sum, const auto& tensor) { return sum + tensor.bytes; });
  Q4T_CHECK(bytes == 224937216);
  Q4T_CHECK(ValidateTtsEncoderTensorHeader(
      Fixture(), TtsConfig::kCodecFileDataBytes));
  auto header = Fixture();
  std::reverse(header.object.begin(), header.object.end());
  Q4T_CHECK(ValidateTtsEncoderTensorHeader(
      header, TtsConfig::kCodecFileDataBytes));
  Q4T_CHECK(!ValidateTtsEncoderTensorHeader(header, bytes));
  return true;
}

Q4T_TEST(tts_encoder_rejects_same_bytes_wrong_graph_and_unknown_names) {
  const std::string norm =
      "encoder.encoder_transformer.layers.7.input_layernorm.bias";
  const std::string projection =
      "encoder.quantizer.acoustic_residual_vector_quantizer.input_proj.weight";
  const std::string unused = "encoder.quantizer."
      "acoustic_residual_vector_quantizer.layers.30.codebook.embed_sum";
  for (const auto& name : {norm, projection, unused}) {
    auto header = Fixture();
    auto& shape = Field(Field(header, name), "shape");
    if (shape.array.size() > 1)
      std::swap(shape.array[0], shape.array[1]);
    else {
      shape.array[0].number /= 2;
      Json two;
      two.type = Json::Type::kNumber;
      two.number = 2;
      shape.array.push_back(two);
    }
    // The old decoder-only check intentionally did not certify this graph.
    Q4T_CHECK(ValidateTtsCodecTensorHeader(
        header, TtsConfig::kCodecFileDataBytes));
    Q4T_CHECK(!ValidateTtsEncoderTensorHeader(
        header, TtsConfig::kCodecFileDataBytes));
    header = Fixture();
    for (auto& [key, value] : header.object)
      if (key == name) key += "_unknown";
    Q4T_CHECK(!ValidateTtsEncoderTensorHeader(
        header, TtsConfig::kCodecFileDataBytes));
  }
  return true;
}

Q4T_TEST(tts_encoder_keeps_full_file_extent_and_decoder_validation) {
  const auto original = Fixture();
  for (const auto* name : {"encoder.downsample.conv.weight",
                           "decoder.pre_conv.conv.weight"}) {
    auto header = original;
    Field(Field(header, name), "dtype").str = "BF16";
    Q4T_CHECK(!ValidateTtsEncoderTensorHeader(
        header, TtsConfig::kCodecFileDataBytes));
    header = original;
    auto& offsets = Field(Field(header, name), "data_offsets").array;
    offsets[0].number += 4;
    offsets[1].number += 4;
    Q4T_CHECK(!ValidateTtsEncoderTensorHeader(
        header, TtsConfig::kCodecFileDataBytes));
    header = original;
    header.object.emplace_back(name, *header.Find(name));
    Q4T_CHECK(!ValidateTtsEncoderTensorHeader(
        header, TtsConfig::kCodecFileDataBytes));
  }
  return true;
}

Q4T_TEST(tts_encoder_layout_rejects_overlap_and_crosses_atomically) {
  TtsWeightLayout layout;
  Q4T_CHECK(ParseTtsEncoderWeightLayout(
      Fixture(), TtsConfig::kCodecFileDataBytes, &layout));
  Q4T_CHECK(layout.bytes == 224937216 && layout.tensors.size() == 225);
  const auto original = layout;
  for (const auto offset : {size_t{0}, TtsConfig::kCodecDecoderBytes - 4,
                            TtsConfig::kCodecFileDataBytes,
                            std::numeric_limits<size_t>::max()}) {
    auto invalid = original;
    invalid.tensors.back().file_offset = offset;
    Q4T_CHECK(!ValidateTtsEncoderWeightLayout(invalid));
  }
  auto invalid = original;
  invalid.tensors.back().file_offset = invalid.tensors.front().file_offset;
  Q4T_CHECK(!ValidateTtsEncoderWeightLayout(invalid));
  invalid = original;
  invalid.tensors.back().offset -= 4;
  Q4T_CHECK(!ValidateTtsEncoderWeightLayout(invalid));
  invalid = original;
  invalid.tensors.back().name += ".extra";
  Q4T_CHECK(!ValidateTtsEncoderWeightLayout(invalid));
  Q4T_CHECK(!ParseTtsEncoderWeightLayout(Fixture(), 123, &layout));
  Q4T_CHECK(layout.bytes == original.bytes &&
            layout.tensors.back().name == original.tensors.back().name);
  Q4T_CHECK(!ParseTtsEncoderWeightLayout(
      Fixture(), TtsConfig::kCodecFileDataBytes, nullptr));
  return true;
}

Q4T_TEST(tts_encoder_binding_keeps_norm_bias_and_split_quantizers) {
  TtsWeightLayout layout;
  Q4T_CHECK(ParseTtsEncoderWeightLayout(
      Fixture(), TtsConfig::kCodecFileDataBytes, &layout));
  const void* base = reinterpret_cast<const void*>(uintptr_t{0x10000000});
  TtsEncoderDeviceWeights w;
  Q4T_CHECK(BindTtsEncoderDeviceWeights(base, layout.bytes, layout, &w));
  Q4T_CHECK(w.input.input == 1 && w.input.output == 64);
  Q4T_CHECK(w.blocks[0].downsample.stride == 4);
  Q4T_CHECK(w.blocks[1].downsample.stride == 5);
  Q4T_CHECK(w.blocks[2].downsample.stride == 6);
  Q4T_CHECK(w.blocks[3].downsample.stride == 8);
  Q4T_CHECK(w.output.input == 1024 && w.output.output == 512);
  Q4T_CHECK(w.downsample.stride == 2 && !w.downsample.bias);
  Q4T_CHECK(w.layers[7].input_norm.bias && w.layers[7].post_norm.bias &&
            w.layers[7].fc2 && w.layers[7].attention_scale);
  Q4T_CHECK(w.quantizer_projection[0] != w.quantizer_projection[1]);
  Q4T_CHECK(w.usage[0] != w.usage[1] && w.usage[1] != w.usage[15]);
  Q4T_CHECK(w.embedding_sum[15] && w.initialized[15]);
  const auto old = w.input.weight;
  Q4T_CHECK(!BindTtsEncoderDeviceWeights(base, layout.bytes - 4, layout, &w));
  Q4T_CHECK(!BindTtsEncoderDeviceWeights(reinterpret_cast<void*>(
      std::numeric_limits<uintptr_t>::max() - 3), layout.bytes, layout, &w));
  Q4T_CHECK(!BindTtsEncoderDeviceWeights(nullptr, layout.bytes, layout, &w));
  Q4T_CHECK(w.input.weight == old);
  return true;
}

Q4T_TEST(tts_encoder_causal_stride_rounds_tail_without_decoder_padding) {
  TtsCodecConvWeights w;
  w.input = 64;
  w.output = 128;
  w.kernel = 8;
  w.stride = 4;
  size_t rows = 777, left = 777;
  for (const auto& [input, expected] :
       {std::pair{1, 1}, {4, 1}, {5, 2}, {1919, 480}, {1921, 481}}) {
    Q4T_CHECK(PlanTtsEncoderConvolution(w, input, &rows, &left));
    Q4T_CHECK(rows == size_t(expected) && left == 4);
  }
  w.kernel = 7;
  w.stride = 1;
  Q4T_CHECK(PlanTtsEncoderConvolution(w, 11, &rows, &left));
  Q4T_CHECK(rows == 11 && left == 6);
  const auto original = w;
  for (int which = 0; which < 5; ++which) {
    w = original;
    if (which == 0) w.stride = 0;
    if (which == 1) w.groups = 2;
    if (which == 2) w.transposed = true;
    if (which == 3) w.kernel = 17;
    if (which == 4) w.dilation = 2;
    Q4T_CHECK(!PlanTtsEncoderConvolution(w, 11, &rows, &left));
    Q4T_CHECK(rows == 11 && left == 6);
  }
  Q4T_CHECK(!PlanTtsEncoderConvolution(original, 0, &rows, &left));
  Q4T_CHECK(!PlanTtsEncoderConvolution(original, kUnlimited, &rows, &left));
  return true;
}

Q4T_TEST(tts_encoder_workspace_charges_wave_attention_and_quantizer) {
  TtsEncoderWorkspace w;
  Q4T_CHECK(PlanTtsEncoderWorkspace(1440000, kUnlimited, &w));
  Q4T_CHECK(w.frames == 750 && w.transformer_rows == 1500 &&
            w.padded_rows == 1504);
  Q4T_CHECK(w.wave_elements == 1440000 * 64);
  Q4T_CHECK(w.wave[1] >= w.wave[0] + w.wave_elements * 4);
  Q4T_CHECK(w.wave[2] >= w.wave[1] + w.wave_elements * 4);
  Q4T_CHECK(w.hidden >= w.wave[2] + w.wave_elements * 4);
  Q4T_CHECK(w.head_output >= w.scores + 1500 * 1504 * 4);
  Q4T_CHECK(w.codebook >= w.residual + 750 * 256 * 4);
  Q4T_CHECK(w.distances >= w.codebook + 2048 * 256 * 4);
  Q4T_CHECK(w.codes >= w.distances + 750 * 2048 * 4);
  Q4T_CHECK(w.columns >= w.codes + 750 * 16 * sizeof(int32_t));
  Q4T_CHECK(w.gemm >= w.columns + TtsCodecConfig::kColumnBytes);
  Q4T_CHECK(w.bytes >= w.gemm + TtsDenseLayout::kGemmBytes + sizeof(int32_t));
  const auto bytes = w.bytes;
  Q4T_CHECK(bytes < (2ULL << 30));
  Q4T_CHECK(PlanTtsEncoderWorkspace(1440000, bytes, &w));
  Q4T_CHECK(!PlanTtsEncoderWorkspace(1440000, bytes - 1, &w));
  Q4T_CHECK(w.bytes == bytes);
  Q4T_CHECK(!PlanTtsEncoderWorkspace(1440001, kUnlimited, &w));
  Q4T_CHECK(!PlanTtsEncoderWorkspace(kUnlimited, kUnlimited, &w));
  Q4T_CHECK(!PlanTtsEncoderWorkspace(0, kUnlimited, &w));
  Q4T_CHECK(!PlanTtsEncoderWorkspace(1, kUnlimited, nullptr));
  return true;
}

Q4T_TEST(tts_encoder_short_and_tail_lengths_cover_each_seanet_stage) {
  for (const size_t samples : {1, 2, 7, 16, 17, 959, 960, 961,
                              1919, 1920, 1921, 24001}) {
    TtsEncoderWorkspace w;
    Q4T_CHECK(PlanTtsEncoderWorkspace(samples, kUnlimited, &w));
    size_t rows = samples, channels = 64;
    Q4T_CHECK(w.wave_elements >= rows * channels);
    for (const auto stride : {4, 5, 6, 8}) {
      rows = (rows + stride - 1) / stride;
      channels *= 2;
      Q4T_CHECK(w.wave_elements >= rows * channels);
    }
    Q4T_CHECK(w.transformer_rows == rows);
    Q4T_CHECK(w.frames == (rows + 1) / 2);
  }
  return true;
}

Q4T_TEST(tts_encoder_pcm_rejects_rate_nonfinite_and_out_of_range) {
  std::vector<float> samples{-1, 0, 1};
  Q4T_CHECK(ValidateTtsEncoderPcm(samples, 24000));
  for (const float value : {std::numeric_limits<float>::infinity(),
                            std::numeric_limits<float>::quiet_NaN(),
                            1.00001f, -1.00001f}) {
    samples[1] = value;
    Q4T_CHECK(!ValidateTtsEncoderPcm(samples, 24000));
  }
  samples[1] = 0;
  Q4T_CHECK(!ValidateTtsEncoderPcm(samples, 16000));
  Q4T_CHECK(!ValidateTtsEncoderPcm({}, 24000));
  samples.resize(1440001);
  Q4T_CHECK(!ValidateTtsEncoderPcm(samples, 24000));
  return true;
}

Q4T_TEST(tts_encoder_config_preserves_causal_layernorm_and_code_counts) {
  const auto original = Fixture("codec-config.json");
  for (const auto* name : {"num_attention_heads", "num_quantizers",
                           "num_semantic_quantizers", "norm_eps",
                           "num_hidden_layers", "sliding_window"}) {
    auto config = original;
    Field(Field(config, "encoder_config"), name).number += 1;
    Q4T_CHECK(!ValidateTtsCodecConfig(config));
  }
  auto config = original;
  Field(Field(config, "encoder_config"), "use_causal_conv").boolean = false;
  Q4T_CHECK(!ValidateTtsCodecConfig(config));
  config = original;
  Field(Field(config, "encoder_config"), "hidden_act").str = "silu";
  Q4T_CHECK(!ValidateTtsCodecConfig(config));
  config = original;
  Field(config, "encoder_valid_num_quantizers").number = 32;
  Q4T_CHECK(!ValidateTtsCodecConfig(config));
  return true;
}
