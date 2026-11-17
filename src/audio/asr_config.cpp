#include "q4t/audio/asr_config.h"

#include <algorithm>
#include <array>
#include <initializer_list>
#include <string_view>
#include <unordered_set>
#include <utility>

namespace q4t::audio {
namespace {
using C = AsrConfig;
using io::Json;
using Numbers = std::initializer_list<std::pair<const char*, double>>;
using Strings = std::initializer_list<std::pair<const char*, const char*>>;
using Bools = std::initializer_list<std::pair<const char*, bool>>;

Status Fail(const std::string& field) {
  return Status::Fail("Qwen3 ASR contract differs: " + field);
}

bool UniqueKeys(const Json& value, int depth = 0) {
  if (depth > 16) return false;
  if (value.IsObject()) {
    std::unordered_set<std::string_view> names;
    for (const auto& [name, child] : value.object)
      if (!names.insert(name).second || !UniqueKeys(child, depth + 1))
        return false;
  } else if (value.IsArray()) {
    for (const auto& child : value.array)
      if (!UniqueKeys(child, depth + 1)) return false;
  }
  return true;
}

Status Fields(const Json& object, const std::string& path, Numbers numbers,
              Strings strings = {}, Bools bools = {}) {
  if (!object.IsObject()) return Fail(path);
  for (const auto& [name, expected] : numbers) {
    const auto* value = object.Find(name);
    if (!value || !value->IsNumber() || value->number != expected)
      return Fail(path + name);
  }
  for (const auto& [name, expected] : strings) {
    const auto* value = object.Find(name);
    if (!value || !value->IsString() || value->str != expected)
      return Fail(path + name);
  }
  for (const auto& [name, expected] : bools) {
    const auto* value = object.Find(name);
    if (!value || !value->IsBool() || value->boolean != expected)
      return Fail(path + name);
  }
  return {};
}

bool Architecture(const Json& config) {
  const auto* array = config.GetArray("architectures");
  return array && array->array.size() == 1 && array->array[0].IsString() &&
         array->array[0].str == "Qwen3ASRForConditionalGeneration";
}

constexpr std::array<std::string_view, 30> kLanguages{
    "Chinese", "English", "Cantonese", "Arabic", "German", "French",
    "Spanish", "Portuguese", "Indonesian", "Italian", "Korean", "Russian",
    "Thai", "Vietnamese", "Japanese", "Turkish", "Hindi", "Malay", "Dutch",
    "Swedish", "Danish", "Finnish", "Polish", "Czech", "Filipino", "Persian",
    "Greek", "Romanian", "Hungarian", "Macedonian"};
}  // namespace

Status ValidateAsrConfig(const Json& config) {
  if (!UniqueKeys(config)) return Fail("duplicate key or depth");
  if (auto s = Fields(config, "", {}, {{"model_type", "qwen3_asr"}}); !s)
    return s;
  if (!Architecture(config)) return Fail("architectures");
  const auto* languages = config.GetArray("support_languages");
  if (!languages || languages->array.size() != kLanguages.size())
    return Fail("support_languages");
  std::unordered_set<std::string_view> seen;
  for (const auto& language : languages->array)
    if (!language.IsString() || !seen.insert(language.str).second ||
        std::find(kLanguages.begin(), kLanguages.end(), language.str) ==
            kLanguages.end())
      return Fail("support_languages");
  const auto* thinker = config.Find("thinker_config");
  if (!thinker || !Architecture(*thinker)) return Fail("thinker_config");
  if (auto s = Fields(*thinker, "thinker.",
                      {{"audio_start_token_id", C::kAudioStart},
                       {"audio_end_token_id", C::kAudioEnd},
                       {"audio_token_id", C::kAudioPad}},
                      {{"model_type", "qwen3_asr"},
                       {"dtype", "bfloat16"}}); !s)
    return s;
  // A classifier checkpoint or alternate outer tying rule changes execution.
  if (thinker->Has("classify_num") || thinker->Has("is_classify") ||
      thinker->Has("tie_word_embeddings"))
    return Fail("thinker execution override");
  const auto* audio = thinker->Find("audio_config");
  const auto* text = thinker->Find("text_config");
  if (!audio || !text) return Fail("encoder/decoder missing");
  if (auto s = Fields(*audio, "audio.",
                      {{"d_model", C::kEncoderHidden},
                       {"encoder_layers", C::kEncoderLayers},
                       {"num_hidden_layers", C::kEncoderLayers},
                       {"encoder_attention_heads", C::kEncoderHeads},
                       {"encoder_ffn_dim", C::kEncoderIntermediate},
                       {"num_mel_bins", C::kMelBins},
                       {"downsample_hidden_size", C::kConvChannels},
                       {"output_dim", C::kHidden},
                       {"n_window", C::kMelChunk / 2},
                       {"n_window_infer", C::kAttentionWindowMel},
                       {"conv_chunksize", 500},
                       {"max_source_positions", C::kSourcePositions},
                       {"dropout", 0}, {"attention_dropout", 0},
                       {"activation_dropout", 0}},
                      {{"model_type", "qwen3_asr_audio_encoder"},
                       {"activation_function", "gelu"}},
                      {{"scale_embedding", false},
                       {"add_cross_attention", false}}); !s)
    return s;
  if (auto s = Fields(*text, "text.",
                      {{"hidden_size", C::kHidden},
                       {"intermediate_size", C::kIntermediate},
                       {"num_hidden_layers", C::kDecoderLayers},
                       {"num_attention_heads", C::kQueryHeads},
                       {"num_key_value_heads", C::kKvHeads},
                       {"head_dim", C::kHeadDim}, {"vocab_size", C::kVocab},
                       {"max_position_embeddings", C::kContext},
                       {"rms_norm_eps", 1e-6}, {"rope_theta", C::kRopeTheta},
                       {"attention_dropout", 0}},
                      {{"model_type", "qwen3"}, {"hidden_act", "silu"}},
                      {{"attention_bias", false}, {"use_cache", true},
                       {"add_cross_attention", false},
                       {"tie_word_embeddings", true}}); !s)
    return s;
  // These optional fields are absent from the pinned checkpoint. Accept only
  // neutral spelling, never silently turn a different attention into full GQA.
  if (const auto* window = text->Find("sliding_window");
      window && !window->IsNull())
    return Fail("text.sliding_window");
  if (const auto* use = text->Find("use_sliding_window");
      use && (!use->IsBool() || use->boolean))
    return Fail("text.use_sliding_window");
  const auto* rope = text->Find("rope_scaling");
  if (!rope) return Fail("text.rope_scaling");
  if (auto s = Fields(*rope, "rope.", {},
                      {{"rope_type", "default"}, {"type", "default"}},
                      {{"interleaved", true}, {"mrope_interleaved", true}});
      !s)
    return s;
  const auto* sections = rope->GetArray("mrope_section");
  if (rope->object.size() != 5 || !sections || sections->array.size() != 3 ||
      sections->array[0].AsInt(-1) != 24 ||
      sections->array[1].AsInt(-1) != 20 ||
      sections->array[2].AsInt(-1) != 20)
    return Fail("rope.mrope_section/extra fields");
  return {};
}

Status ValidateAsrPreprocessorConfig(const Json& config) {
  if (!UniqueKeys(config)) return Fail("preprocessor duplicate key or depth");
  if (auto s = Fields(config, "preprocessor.",
                      {{"feature_size", C::kMelBins}, {"hop_length", C::kHop},
                       {"n_fft", C::kFft}, {"chunk_length", 30},
                       {"n_samples", 480000}, {"nb_max_frames", 3000},
                       {"dither", 0}, {"padding_value", 0}},
                      {{"feature_extractor_type", "WhisperFeatureExtractor"},
                       {"processor_class", "Qwen3ASRProcessor"},
                       {"padding_side", "right"}},
                      {{"return_attention_mask", true}}); !s)
    return s;
  // The official asset omits sampling_rate and relies on Whisper's 16 kHz
  // default. Explicit metadata must agree; no resampling by reinterpretation.
  if (config.Has("sampling_rate"))
    return Fields(config, "preprocessor.",
                  {{"sampling_rate", C::kSampleRate}});
  return {};
}

Status ValidateAsrGenerationConfig(const Json& config) {
  if (!UniqueKeys(config)) return Fail("generation duplicate key or depth");
  if (auto s = Fields(config, "generation.",
                      {{"pad_token_id", C::kEos}, {"temperature", 1e-6}}, {},
                      {{"do_sample", false}, {"_from_model_config", true}});
      !s)
    return s;
  const auto* eos = config.GetArray("eos_token_id");
  if (!eos || eos->array.size() != 2 ||
      eos->array[0].AsInt(-1) != C::kEos ||
      eos->array[1].AsInt(-1) != C::kImEnd || config.object.size() != 5)
    return Fail("generation.eos/extra fields");
  return {};
}

std::vector<AsrTensorContract> AsrTensorContracts() {
  std::vector<AsrTensorContract> result;
  result.reserve(612);
  const auto add = [&](std::string name, std::vector<int64_t> shape) {
    size_t bytes = sizeof(uint16_t);
    for (const auto extent : shape) bytes *= static_cast<size_t>(extent);
    result.push_back({std::move(name), std::move(shape), bytes});
  };
  const std::string audio = "thinker.audio_tower.";
  for (int i = 1; i <= 3; ++i) {
    const auto name = audio + "conv2d" + std::to_string(i);
    add(name + ".weight", {C::kConvChannels,
                           i == 1 ? 1 : C::kConvChannels, 3, 3});
    add(name + ".bias", {C::kConvChannels});
  }
  add(audio + "conv_out.weight",
      {C::kEncoderHidden, C::kConvChannels * C::kConvFrequency});
  for (int i = 0; i < C::kEncoderLayers; ++i) {
    const auto prefix = audio + "layers." + std::to_string(i) + ".";
    for (const char* norm : {"self_attn_layer_norm", "final_layer_norm"})
      for (const char* part : {".weight", ".bias"})
        add(prefix + norm + part, {C::kEncoderHidden});
    for (const char* proj : {"q_proj", "k_proj", "v_proj", "out_proj"}) {
      add(prefix + "self_attn." + proj + ".weight",
          {C::kEncoderHidden, C::kEncoderHidden});
      add(prefix + "self_attn." + proj + ".bias", {C::kEncoderHidden});
    }
    add(prefix + "fc1.weight", {C::kEncoderIntermediate, C::kEncoderHidden});
    add(prefix + "fc1.bias", {C::kEncoderIntermediate});
    add(prefix + "fc2.weight", {C::kEncoderHidden, C::kEncoderIntermediate});
    add(prefix + "fc2.bias", {C::kEncoderHidden});
  }
  for (const char* part : {".weight", ".bias"})
    add(audio + "ln_post" + part, {C::kEncoderHidden});
  add(audio + "proj1.weight", {C::kEncoderHidden, C::kEncoderHidden});
  add(audio + "proj1.bias", {C::kEncoderHidden});
  add(audio + "proj2.weight", {C::kHidden, C::kEncoderHidden});
  add(audio + "proj2.bias", {C::kHidden});
  // Both tensors really exist. Nested text_config.tie_word_embeddings does
  // not authorize aliasing the outer thinker's stored output head.
  add("thinker.model.embed_tokens.weight", {C::kVocab, C::kHidden});
  add("thinker.lm_head.weight", {C::kVocab, C::kHidden});
  add("thinker.model.norm.weight", {C::kHidden});
  for (int i = 0; i < C::kDecoderLayers; ++i) {
    const auto prefix = "thinker.model.layers." + std::to_string(i) + ".";
    for (const char* norm : {"input_layernorm", "post_attention_layernorm"})
      add(prefix + norm + ".weight", {C::kHidden});
    for (const char* norm : {"q_norm", "k_norm"})
      add(prefix + "self_attn." + norm + ".weight", {C::kHeadDim});
    add(prefix + "self_attn.q_proj.weight",
        {C::kQueryHeads * C::kHeadDim, C::kHidden});
    for (const char* proj : {"k_proj", "v_proj"})
      add(prefix + "self_attn." + proj + ".weight",
          {C::kKvHeads * C::kHeadDim, C::kHidden});
    add(prefix + "self_attn.o_proj.weight",
        {C::kHidden, C::kQueryHeads * C::kHeadDim});
    for (const char* proj : {"gate_proj", "up_proj"})
      add(prefix + "mlp." + proj + ".weight",
          {C::kIntermediate, C::kHidden});
    add(prefix + "mlp.down_proj.weight", {C::kHidden, C::kIntermediate});
  }
  return result;
}

size_t AsrWeightBytes() {
  size_t bytes = 0;
  for (const auto& tensor : AsrTensorContracts()) bytes += tensor.bytes;
  return bytes;
}

size_t AsrEncodedFrames(size_t mel_frames) {
  const size_t tail = mel_frames % C::kMelChunk;
  // Three ceil-divide-by-two convolutions on at most 99 tail frames.
  return mel_frames / C::kMelChunk * C::kEncodedChunk + (tail + 7) / 8;
}

Status ValidateAsrTensorHeader(const Json& header, uint64_t data_bytes) {
  if (!header.IsObject() || !UniqueKeys(header))
    return Fail("tensor header object/duplicate/depth");
  const auto contracts = AsrTensorContracts();
  if (data_bytes != AsrWeightBytes()) return Fail("tensor data bytes");
  const auto* metadata = header.Find("__metadata__");
  if (header.object.size() != contracts.size() + (metadata ? 1 : 0))
    return Fail("tensor count");
  if (metadata) {
    if (!metadata->IsObject()) return Fail("tensor metadata object");
    for (const auto& [name, value] : metadata->object) {
      (void)name;
      if (!value.IsString()) return Fail("tensor metadata value");
    }
  }
  std::vector<std::pair<uint64_t, uint64_t>> extents;
  extents.reserve(contracts.size());
  for (const auto& expected : contracts) {
    const auto* actual = header.Find(expected.name);
    if (!actual || !actual->IsObject() || actual->object.size() != 3 ||
        actual->GetString("dtype") != "BF16")
      return Fail(expected.name + " dtype/fields");
    const auto* shape = actual->GetArray("shape");
    if (!shape || shape->array.size() != expected.shape.size())
      return Fail(expected.name + " shape");
    for (size_t i = 0; i < expected.shape.size(); ++i)
      if (shape->array[i].AsInt(-1) != expected.shape[i])
        return Fail(expected.name + " dimension");
    const auto* offsets = actual->GetArray("data_offsets");
    if (!offsets || offsets->array.size() != 2)
      return Fail(expected.name + " offsets");
    const auto start = offsets->array[0].AsInt(-1);
    const auto end = offsets->array[1].AsInt(-1);
    if (start < 0 || end < start || static_cast<uint64_t>(end) > data_bytes ||
        static_cast<uint64_t>(end - start) != expected.bytes)
      return Fail(expected.name + " bounds");
    extents.emplace_back(start, end);
  }
  std::sort(extents.begin(), extents.end());
  uint64_t next = 0;
  for (const auto& [start, end] : extents) {
    if (start != next) return Fail("tensor overlap/hole");
    next = end;
  }
  if (next != data_bytes) return Fail("tensor trailing data");
  return {};
}

}  // namespace q4t::audio
