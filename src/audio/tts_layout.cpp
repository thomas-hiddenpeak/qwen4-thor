#include "q4t/audio/tts_layout.h"

#include <algorithm>
#include <bit>
#include <cmath>
#include <limits>
#include <map>
#include <utility>

#include "q4t/audio/tts_generation.h"
#include "q4t/audio/tts_speaker_layout.h"

namespace q4t::audio {
namespace {

bool Variant(TtsVariant variant) {
  return variant == TtsVariant::kCustomVoice06B ||
         variant == TtsVariant::kCustomVoice17B ||
         variant == TtsVariant::kBase17B;
}

Status Pack(const io::Json& header, size_t bytes,
            const std::vector<TtsTensorContract>& tensors,
            TtsWeightLayout* out) {
  if (!out) return Status::Fail("TTS weight output is null");
  TtsWeightLayout layout;
  layout.file_bytes = bytes;
  for (const auto& spec : tensors) {
    const auto start =
        header.Find(spec.name)->GetArray("data_offsets")->array[0].AsInt(-1);
    layout.tensors.push_back(
        {spec.name, static_cast<size_t>(start), layout.bytes, spec.bytes});
    layout.bytes += spec.bytes;
  }
  *out = std::move(layout);
  return {};
}

}  // namespace

Status ParseTtsWeightLayout(const io::Json& header, size_t bytes,
                            TtsVariant variant, TtsWeightLayout* out) {
  if (auto s = ValidateTtsTensorHeader(header, bytes, variant); !s) return s;
  return Pack(header, bytes, TtsTensorContracts(variant), out);
}

Status ParseTtsCodecWeightLayout(const io::Json& header, size_t bytes,
                                 TtsWeightLayout* out) {
  if (auto s = ValidateTtsCodecTensorHeader(header, bytes); !s) return s;
  return Pack(header, bytes, TtsCodecDecoderTensorContracts(), out);
}

Status ValidateTtsWeightLayout(const TtsWeightLayout& layout,
                               TtsVariant variant, bool codec) {
  if (!Variant(variant)) return Status::Fail("TTS variant differs");
  const auto specs =
      codec ? TtsCodecDecoderTensorContracts() : TtsTensorContracts(variant);
  if (layout.tensors.size() != specs.size())
    return Status::Fail("TTS device tensor count differs");
  size_t bytes = 0;
  std::vector<std::pair<size_t, size_t>> intervals;
  for (size_t i = 0; i < specs.size(); ++i) {
    const auto& t = layout.tensors[i];
    if (t.name != specs[i].name || t.bytes != specs[i].bytes ||
        t.offset != bytes || t.file_offset % (codec ? 4 : 2) ||
        t.file_offset > layout.file_bytes ||
        t.bytes > layout.file_bytes - t.file_offset)
      return Status::Fail("TTS device tensor extent differs");
    intervals.emplace_back(t.file_offset, t.file_offset + t.bytes);
    bytes += t.bytes;
  }
  const bool base = !codec && variant == TtsVariant::kBase17B;
  const size_t prefix = base ? TtsSpeakerConfig::kSourceBytes : 0;
  if (layout.bytes != bytes ||
      layout.file_bytes !=
          (codec ? TtsConfig::kCodecFileDataBytes : bytes + prefix))
    return Status::Fail("TTS weight storage differs");
  std::sort(intervals.begin(), intervals.end());
  size_t end = prefix;
  for (const auto& interval : intervals) {
    if (interval.first < end || (!codec && interval.first != end))
      return Status::Fail("TTS weight overlap or gap");
    end = interval.second;
  }
  return {};
}

Status ExpandTtsWeightChunk(std::span<const uint16_t> input,
                            std::span<float> output) {
  const auto source = reinterpret_cast<uintptr_t>(input.data());
  const auto target = reinterpret_cast<uintptr_t>(output.data());
  if (input.empty() || input.size() > (1U << 20) ||
      input.size() != output.size() || !source || !target ||
      source % alignof(uint16_t) || target % alignof(float) ||
      source > std::numeric_limits<uintptr_t>::max() - input.size_bytes() ||
      target > std::numeric_limits<uintptr_t>::max() - output.size_bytes() ||
      (source < target + output.size_bytes() &&
       target < source + input.size_bytes()))
    return Status::Fail("TTS weight expansion buffers invalid");
  const auto value = [&](size_t i) {
    return std::bit_cast<float>(static_cast<uint32_t>(input[i]) << 16);
  };
  for (size_t i = 0; i < input.size(); ++i)
    if (!std::isfinite(value(i)))
      return Status::Fail("TTS weight is not finite");
  for (size_t i = 0; i < input.size(); ++i) output[i] = value(i);
  return {};
}

Status BindTtsDeviceWeights(const void* data, size_t bytes,
                            const TtsWeightLayout& layout, TtsVariant variant,
                            TtsDeviceWeights* out) {
  const auto base = reinterpret_cast<uintptr_t>(data);
  if (!data || !out || base % alignof(float) ||
      layout.bytes > std::numeric_limits<size_t>::max() / 2 ||
      bytes != layout.bytes * 2 ||
      base > std::numeric_limits<uintptr_t>::max() - bytes)
    return Status::Fail("TTS device buffer bounds invalid");
  if (auto s = ValidateTtsWeightLayout(layout, variant); !s) return s;
  std::map<std::string, const float*> pointers;
  for (const auto& t : layout.tensors)
    pointers.emplace(t.name,
                     reinterpret_cast<const float*>(base + t.offset * 2));
  const auto affine = [&](const std::string& name) {
    return TtsAffineWeights{pointers.at(name + ".weight"),
                            pointers.at(name + ".bias")};
  };
  const auto dense = [&](const std::string& name, int layers) {
    TtsDenseWeights result;
    result.norm = pointers.at(name + ".norm.weight");
    for (int i = 0; i < layers; ++i) {
      auto& w = result.layers[i];
      const auto p = name + ".layers." + std::to_string(i) + ".";
      w.input_norm = pointers.at(p + "input_layernorm.weight");
      w.post_norm = pointers.at(p + "post_attention_layernorm.weight");
      w.q_norm = pointers.at(p + "self_attn.q_norm.weight");
      w.k_norm = pointers.at(p + "self_attn.k_norm.weight");
      w.q = pointers.at(p + "self_attn.q_proj.weight");
      w.k = pointers.at(p + "self_attn.k_proj.weight");
      w.v = pointers.at(p + "self_attn.v_proj.weight");
      w.o = pointers.at(p + "self_attn.o_proj.weight");
      w.gate = pointers.at(p + "mlp.gate_proj.weight");
      w.up = pointers.at(p + "mlp.up_proj.weight");
      w.down = pointers.at(p + "mlp.down_proj.weight");
    }
    return result;
  };
  TtsDeviceWeights w;
  w.variant = variant;
  w.text_embedding = pointers.at("talker.model.text_embedding.weight");
  w.codec_embedding = pointers.at("talker.model.codec_embedding.weight");
  w.codec_head = pointers.at("talker.codec_head.weight");
  w.text_fc1 = affine("talker.text_projection.linear_fc1");
  w.text_fc2 = affine("talker.text_projection.linear_fc2");
  w.talker = dense("talker.model", 28);
  w.predictor = dense("talker.code_predictor.model", 5);
  if (variant != TtsVariant::kCustomVoice06B)
    w.predictor_projection =
        affine("talker.code_predictor.small_to_mtp_projection");
  for (int i = 0; i < 15; ++i) {
    const auto p = "talker.code_predictor.";
    w.code_embeddings[i] =
        pointers.at(p + std::string("model.codec_embedding.") +
                    std::to_string(i) + ".weight");
    w.code_heads[i] = pointers.at(p + std::string("lm_head.") +
                                  std::to_string(i) + ".weight");
  }
  *out = w;
  return {};
}

Status PlanTtsDenseWorkspace(TtsVariant variant, bool predictor, size_t rows,
                             size_t context, size_t max_bytes,
                             TtsDenseLayout* out) {
  if (!out || !Variant(variant) || rows == 0 || rows > context ||
      context > (predictor ? 16 : 32768))
    return Status::Fail("TTS dense dimensions exceed context");
  TtsDenseLayout w;
  w.hidden = predictor || variant == TtsVariant::kCustomVoice06B ? 1024 : 2048;
  w.intermediate = w.hidden * 3;
  w.layers = predictor ? 5 : 28;
  w.rows = static_cast<int>(rows);
  w.context = static_cast<int>(context);
  w.padded_rows = (w.rows + 15) / 16 * 16;
  w.padded_context = (w.context + 15) / 16 * 16;
  // All products have fixed maxima before conversion/allocation arithmetic.
  const auto carve = [&](size_t bytes) {
    const size_t start = (w.bytes + 255) / 256 * 256;
    w.bytes = start + bytes;
    return start;
  };
  const auto fp32 = [&](size_t n) { return carve(n * sizeof(float)); };
  w.hidden_state = fp32(rows * w.hidden);
  w.normalized = fp32(rows * w.hidden);
  w.projected = fp32(rows * w.hidden);
  w.q = fp32(rows * 2048);
  w.k = fp32(rows * 1024);
  w.v = fp32(rows * 1024);
  w.head_q = fp32(static_cast<size_t>(w.padded_rows) * 2048);
  w.rope = fp32(rows * 128);
  w.scores = fp32(rows * w.padded_context);
  w.head_output = fp32(rows * 128);
  w.attention = fp32(rows * 2048);
  w.gate = fp32(rows * w.intermediate);
  w.up = fp32(rows * w.intermediate);
  const size_t cache = static_cast<size_t>(w.layers) * w.padded_context * 1024;
  w.cache_k = fp32(cache);
  w.cache_v = fp32(cache);
  w.gemm = carve(TtsDenseLayout::kGemmBytes);
  if (w.bytes > max_bytes) return Status::Fail("TTS dense RAM budget exceeded");
  *out = w;
  return {};
}

Status PlanTtsGenerationWorkspace(TtsVariant variant, size_t text_tokens,
                                  size_t prefill_tokens, size_t max_frames,
                                  size_t max_bytes, TtsGenerationLayout* out) {
  if (!out || text_tokens < 4 || text_tokens > 32768 || max_frames < 2 ||
      max_frames > 4096 || prefill_tokens < 8 ||
      prefill_tokens > 32768 - max_frames)
    return Status::Fail("TTS text, prompt or generation budget invalid");
  TtsGenerationLayout w;
  if (auto s = PlanTtsDenseWorkspace(variant, false, prefill_tokens,
                                     prefill_tokens + max_frames, max_bytes,
                                     &w.talker);
      !s)
    return s;
  if (auto s =
          PlanTtsDenseWorkspace(variant, true, 2, 16, max_bytes, &w.predictor);
      !s)
    return s;
  const auto carve = [&](size_t bytes) {
    const size_t start = (w.bytes + 255) / 256 * 256;
    w.bytes = start + bytes;
    return start;
  };
  const auto fp32 = [&](size_t n) { return carve(n * sizeof(float)); };
  const size_t hidden = w.talker.hidden;
  w.talker_offset = carve(w.talker.bytes);
  w.predictor_offset = carve(w.predictor.bytes);
  w.ids = carve(std::max(text_tokens, size_t{16}) * sizeof(int32_t));
  w.text_lookup = fp32(text_tokens * 2048);
  w.text_intermediate = fp32(text_tokens * 2048);
  w.text_projected = fp32(text_tokens * hidden);
  w.affine_fp32 = carve(text_tokens * 2048 * sizeof(float));
  w.prompt_rows = carve(prefill_tokens * sizeof(TtsPromptRow));
  if (variant == TtsVariant::kBase17B) w.speaker_condition = fp32(2048);
  w.prefill = fp32(prefill_tokens * hidden);
  w.past_hidden = fp32(hidden);
  w.codec_embeddings = fp32(16 * hidden);
  w.decode_input = fp32(hidden);
  w.predictor_input = fp32(2 * hidden);
  w.predictor_projected = fp32(2 * 1024);
  w.logits = fp32(3072);
  if (w.bytes > max_bytes)
    return Status::Fail("TTS generation RAM budget exceeded");
  *out = w;
  return {};
}

}  // namespace q4t::audio
