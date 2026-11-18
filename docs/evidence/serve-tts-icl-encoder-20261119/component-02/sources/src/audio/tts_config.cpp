#include "q4t/audio/tts_config.h"

#include <algorithm>
#include <cmath>
#include <map>
#include <set>
#include <span>
#include <string_view>

#include "tts_base_config_data.h"
#include "tts_config_data.h"

namespace q4t::audio {
namespace {
using io::Json;

Status Fail(const std::string& path) {
  return Status::Fail("Qwen3 TTS contract differs: " + path);
}

bool ValidVariant(TtsVariant variant) {
  return variant == TtsVariant::kCustomVoice06B ||
         variant == TtsVariant::kCustomVoice17B ||
         variant == TtsVariant::kBase17B;
}

// Configuration is a fixed checkpoint contract, not a permissive model-class
// factory. Compare by key, preserving null/boolean/number distinctions. Reject
// new execution overrides as well as changed known parameters.
Status EqualConfig(const Json& actual, const Json& expected,
                   const std::string& path, int depth = 0) {
  if (depth > 16 || actual.type != expected.type) return Fail(path);
  if (actual.IsObject()) {
    if (actual.object.size() != expected.object.size()) return Fail(path);
    std::set<std::string_view> names;
    for (const auto& [name, value] : actual.object) {
      const auto* wanted = expected.Find(name);
      if (!names.insert(name).second || !wanted) return Fail(path + name);
      // Exporter provenance cannot change the actual compute graph.
      if (name == "transformers_version" || name == "_name_or_path") {
        if (!value.IsString()) return Fail(path + name);
        continue;
      }
      if (auto status =
              EqualConfig(value, *wanted, path + name + ".", depth + 1);
          !status)
        return status;
    }
  } else if (actual.IsArray()) {
    if (actual.array.size() != expected.array.size()) return Fail(path);
    for (size_t i = 0; i < actual.array.size(); ++i)
      if (auto status = EqualConfig(actual.array[i], expected.array[i],
                                    path + std::to_string(i) + ".", depth + 1);
          !status)
        return status;
  } else if (actual.IsNumber()) {
    if (!std::isfinite(actual.number) || actual.number != expected.number)
      return Fail(path);
  } else if (actual.IsBool()) {
    if (actual.boolean != expected.boolean) return Fail(path);
  } else if (actual.IsString() && actual.str != expected.str) {
    return Fail(path);
  }
  return {};
}

Status Config(const Json& actual, std::string_view contract) {
  Json expected;
  if (!io::ParseJson(std::string(contract), &expected, false,
                     {.reject_duplicate_keys = true}))
    return Fail("internal fixed metadata");
  return EqualConfig(actual, expected, "");
}

class TensorGraph {
 public:
  explicit TensorGraph(size_t element_bytes) : element_bytes_(element_bytes) {}

  void Add(std::string name, std::vector<int64_t> shape) {
    size_t bytes = element_bytes_;
    for (const auto dimension : shape) bytes *= dimension;
    tensors_.push_back({std::move(name), std::move(shape), bytes});
  }

  void Linear(const std::string& name, int in, int out, bool bias = false) {
    Add(name + ".weight", {out, in});
    if (bias) Add(name + ".bias", {out});
  }

  void Conv(const std::string& name, int in, int out, int kernel,
            bool bias = true, bool transpose = false) {
    Add(name + ".weight", transpose ? std::vector<int64_t>{in, out, kernel}
                                    : std::vector<int64_t>{out, in, kernel});
    if (bias) Add(name + ".bias", {out});
  }

  void TalkerLayers(const std::string& base, int layers, int hidden, int ffn) {
    for (int i = 0; i < layers; ++i) {
      const auto p = base + ".layers." + std::to_string(i) + ".";
      for (const auto* name : {"input_layernorm", "post_attention_layernorm"})
        Add(p + name + ".weight", {hidden});
      for (const auto* name : {"q_norm", "k_norm"})
        Add(p + "self_attn." + name + ".weight", {128});
      Linear(p + "self_attn.q_proj", hidden, 2048);
      Linear(p + "self_attn.k_proj", hidden, 1024);
      Linear(p + "self_attn.v_proj", hidden, 1024);
      Linear(p + "self_attn.o_proj", 2048, hidden);
      Linear(p + "mlp.gate_proj", hidden, ffn);
      Linear(p + "mlp.up_proj", hidden, ffn);
      Linear(p + "mlp.down_proj", ffn, hidden);
    }
  }

  void Snake(const std::string& name, int channels) {
    Add(name + ".alpha", {channels});
    Add(name + ".beta", {channels});
  }

  std::vector<TtsTensorContract> Finish() {
    std::sort(tensors_.begin(), tensors_.end(),
              [](const auto& a, const auto& b) { return a.name < b.name; });
    return std::move(tensors_);
  }

 private:
  size_t element_bytes_;
  std::vector<TtsTensorContract> tensors_;
};

bool Integer(const Json& value, uint64_t maximum, uint64_t* out) {
  if (!value.IsNumber() || !std::isfinite(value.number) || value.number < 0 ||
      value.number > static_cast<double>(maximum) ||
      std::floor(value.number) != value.number)
    return false;
  *out = static_cast<uint64_t>(value.number);
  return true;
}

Status Header(const Json& header, uint64_t data_bytes,
              std::span<const TtsTensorContract> contracts, bool codec) {
  if (!header.IsObject()) return Fail("tensor header");
  uint64_t expected_bytes = 0;
  std::map<std::string_view, const TtsTensorContract*> expected;
  for (const auto& tensor : contracts) {
    expected_bytes += tensor.bytes;
    expected.emplace(tensor.name, &tensor);
  }
  if (contracts.empty() ||
      data_bytes != (codec ? TtsConfig::kCodecFileDataBytes : expected_bytes))
    return Fail("tensor storage size");
  std::set<std::string_view> seen;
  std::vector<std::pair<uint64_t, uint64_t>> intervals;
  size_t inactive = 0;
  for (const auto& [name, tensor] : header.object) {
    if (!seen.insert(name).second) return Fail("duplicate tensor " + name);
    if (name == "__metadata__") {
      if (!tensor.IsObject()) return Fail("tensor metadata");
      std::set<std::string_view> metadata_names;
      for (const auto& [key, value] : tensor.object)
        if (!value.IsString() || !metadata_names.insert(key).second)
          return Fail("tensor metadata value");
      continue;
    }
    const auto it = expected.find(name);
    if (it == expected.end()) {
      if (!codec || !name.starts_with("encoder.")) return Fail(name);
      ++inactive;
    }
    if (!tensor.IsObject() || tensor.object.size() != 3 ||
        tensor.GetString("dtype") != (codec ? "F32" : "BF16"))
      return Fail(name + ".dtype/fields");
    const auto* shape = tensor.GetArray("shape");
    const auto* offsets = tensor.GetArray("data_offsets");
    if (!shape || shape->array.size() > 4 || !offsets ||
        offsets->array.size() != 2)
      return Fail(name + ".shape/offsets");
    uint64_t bytes = codec ? 4 : 2;
    if (it != expected.end() && shape->array.size() != it->second->shape.size())
      return Fail(name + ".rank");
    for (size_t i = 0; i < shape->array.size(); ++i) {
      uint64_t dimension;
      if (!Integer(shape->array[i], data_bytes / bytes, &dimension) ||
          !dimension ||
          (it != expected.end() &&
           dimension != static_cast<uint64_t>(it->second->shape[i])))
        return Fail(name + ".dimension");
      bytes *= dimension;
    }
    uint64_t start, end;
    if (!Integer(offsets->array[0], data_bytes, &start) ||
        !Integer(offsets->array[1], data_bytes, &end) || end < start ||
        end - start != bytes || start % (codec ? 4 : 2))
      return Fail(name + ".extent");
    intervals.emplace_back(start, end);
    if (it != expected.end()) expected.erase(it);
  }
  if (!expected.empty() || inactive != (codec ? 225U : 0U))
    return Fail("missing tensors or inactive encoder count");
  std::sort(intervals.begin(), intervals.end());
  uint64_t next = 0;
  for (const auto& [start, end] : intervals) {
    if (start != next) return Fail("tensor overlap/hole");
    next = end;
  }
  return next == data_bytes ? Status{} : Fail("trailing storage");
}
}  // namespace

Status ValidateTtsConfig(const Json& config, TtsVariant variant) {
  if (!ValidVariant(variant)) return Fail("variant");
  if (variant == TtsVariant::kBase17B) return ValidateTtsBaseConfig(config);
  return Config(config, variant == TtsVariant::kCustomVoice06B
                            ? tts_detail::kConfig06B
                            : tts_detail::kConfig17B);
}

Status ValidateTtsBaseConfig(const Json& config) {
  return Config(config, tts_detail::kBaseConfig17B);
}

std::vector<TtsTensorContract> TtsBaseTensorContracts() {
  // The Base talker/predictor shapes match the 1.7B graph. Weight identity
  // remains separate: shape equality never permits reusing CustomVoice data.
  auto tensors = TtsTensorContracts(TtsVariant::kCustomVoice17B);
  TensorGraph graph(2);
  const std::string prefix = "speaker_encoder.";
  graph.Conv(prefix + "blocks.0.conv", 128, 512, 5);
  for (int block = 1; block <= 3; ++block) {
    const auto p = prefix + "blocks." + std::to_string(block) + ".";
    graph.Conv(p + "tdnn1.conv", 512, 512, 1);
    graph.Conv(p + "tdnn2.conv", 512, 512, 1);
    for (int split = 0; split < 7; ++split)
      graph.Conv(p + "res2net_block.blocks." + std::to_string(split) + ".conv",
                 64, 64, 3);
    graph.Conv(p + "se_block.conv1", 512, 128, 1);
    graph.Conv(p + "se_block.conv2", 128, 512, 1);
  }
  graph.Conv(prefix + "mfa.conv", 1536, 1536, 1);
  graph.Conv(prefix + "asp.tdnn.conv", 4608, 128, 1);
  graph.Conv(prefix + "asp.conv", 128, 1536, 1);
  graph.Conv(prefix + "fc", 3072, 2048, 1);
  auto speaker = graph.Finish();
  tensors.insert(tensors.end(), speaker.begin(), speaker.end());
  return tensors;
}

Status ValidateTtsBaseTensorHeader(const Json& header, uint64_t data_bytes) {
  return Header(header, data_bytes, TtsBaseTensorContracts(), false);
}

Status ValidateTtsGenerationConfig(const Json& config) {
  return Config(config, tts_detail::kGeneration);
}

Status ValidateTtsCodecConfig(const Json& config) {
  return Config(config, tts_detail::kCodecConfig);
}

std::vector<TtsTensorContract> TtsTensorContracts(TtsVariant variant) {
  if (!ValidVariant(variant)) return {};
  const int hidden = variant == TtsVariant::kCustomVoice06B ? 1024 : 2048;
  TensorGraph graph(2);
  graph.Add("talker.model.text_embedding.weight", {151936, 2048});
  graph.Add("talker.model.codec_embedding.weight", {3072, hidden});
  graph.Linear("talker.codec_head", hidden, 3072);
  graph.Add("talker.model.norm.weight", {hidden});
  graph.Linear("talker.text_projection.linear_fc1", 2048, 2048, true);
  graph.Linear("talker.text_projection.linear_fc2", 2048, hidden, true);
  graph.TalkerLayers("talker.model", 28, hidden, hidden * 3);
  const std::string p = "talker.code_predictor.";
  graph.Add(p + "model.norm.weight", {1024});
  if (hidden != 1024)
    graph.Linear(p + "small_to_mtp_projection", hidden, 1024, true);
  graph.TalkerLayers(p + "model", 5, 1024, 3072);
  for (int i = 0; i < 15; ++i) {
    // Predictor input tables use TALKER width even when its hidden state is
    // 1024. The 1.7B checkpoint applies small_to_mtp_projection afterwards.
    graph.Add(p + "model.codec_embedding." + std::to_string(i) + ".weight",
              {2048, hidden});
    graph.Linear(p + "lm_head." + std::to_string(i), 1024, 2048);
  }
  return graph.Finish();
}

std::vector<TtsTensorContract> TtsCodecDecoderTensorContracts() {
  TensorGraph g(4);
  for (const auto* group : {"rvq_first", "rvq_rest"}) {
    const auto p = std::string("decoder.quantizer.") + group + ".";
    g.Conv(p + "input_proj", 512, 256, 1, false);
    g.Conv(p + "output_proj", 256, 512, 1, false);
    const int count = std::string_view(group) == "rvq_first" ? 1 : 15;
    for (int i = 0; i < count; ++i) {
      const auto base = p + "vq.layers." + std::to_string(i) + "._codebook.";
      g.Add(base + "cluster_usage", {2048});
      g.Add(base + "embedding_sum", {2048, 256});
    }
  }
  g.Conv("decoder.pre_conv.conv", 512, 1024, 3);
  const std::string p = "decoder.pre_transformer.";
  g.Linear(p + "input_proj", 1024, 512, true);
  g.Linear(p + "output_proj", 512, 1024, true);
  g.Add(p + "norm.weight", {512});
  for (int i = 0; i < 8; ++i) {
    const auto layer = p + "layers." + std::to_string(i) + ".";
    g.Add(layer + "input_layernorm.weight", {512});
    g.Add(layer + "post_attention_layernorm.weight", {512});
    for (const auto* name : {"q_proj", "k_proj", "v_proj"})
      g.Linear(layer + "self_attn." + name, 512, 1024);
    g.Linear(layer + "self_attn.o_proj", 1024, 512);
    g.Linear(layer + "mlp.gate_proj", 512, 1024);
    g.Linear(layer + "mlp.up_proj", 512, 1024);
    g.Linear(layer + "mlp.down_proj", 1024, 512);
    g.Add(layer + "self_attn_layer_scale.scale", {512});
    g.Add(layer + "mlp_layer_scale.scale", {512});
  }
  for (int i = 0; i < 2; ++i) {
    const auto base = "decoder.upsample." + std::to_string(i) + ".";
    g.Conv(base + "0.conv", 1024, 1024, 2, true, true);
    g.Conv(base + "1.dwconv.conv", 1, 1024, 7);
    for (const auto* name : {"weight", "bias"})
      g.Add(base + "1.norm." + name, {1024});
    g.Linear(base + "1.pwconv1", 1024, 4096, true);
    g.Linear(base + "1.pwconv2", 4096, 1024, true);
    g.Add(base + "1.gamma", {1024});
  }
  g.Conv("decoder.decoder.0.conv", 1024, 1536, 7);
  constexpr int rates[] = {8, 5, 4, 3};
  for (int i = 0; i < 4; ++i) {
    const int in = 1536 >> i, out = in / 2;
    const auto base = "decoder.decoder." + std::to_string(i + 1) + ".block.";
    g.Snake(base + "0", in);
    g.Conv(base + "1.conv", in, out, rates[i] * 2, true, true);
    for (int j = 2; j < 5; ++j) {
      const auto unit = base + std::to_string(j) + ".";
      g.Snake(unit + "act1", out);
      g.Snake(unit + "act2", out);
      g.Conv(unit + "conv1.conv", out, out, 7);
      g.Conv(unit + "conv2.conv", out, out, 1);
    }
  }
  g.Snake("decoder.decoder.5", 96);
  g.Conv("decoder.decoder.6.conv", 96, 1, 7);
  return g.Finish();
}

Status ValidateTtsTensorHeader(const Json& header, uint64_t data_bytes,
                               TtsVariant variant) {
  if (!ValidVariant(variant)) return Fail("variant");
  if (variant == TtsVariant::kBase17B)
    return ValidateTtsBaseTensorHeader(header, data_bytes);
  return Header(header, data_bytes, TtsTensorContracts(variant), false);
}

Status ValidateTtsCodecTensorHeader(const Json& header, uint64_t data_bytes) {
  return Header(header, data_bytes, TtsCodecDecoderTensorContracts(), true);
}

}  // namespace q4t::audio
