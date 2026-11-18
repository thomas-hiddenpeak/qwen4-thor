#include "q4t/audio/tts_layout.h"

#include <algorithm>
#include <utility>

namespace q4t::audio {
namespace {

bool Variant(TtsVariant variant) {
  return variant == TtsVariant::kCustomVoice06B ||
         variant == TtsVariant::kCustomVoice17B;
}

Status Pack(const io::Json& header, size_t bytes,
            const std::vector<TtsTensorContract>& tensors,
            TtsWeightLayout* out) {
  if (!out) return Status::Fail("TTS weight output is null");
  TtsWeightLayout layout;
  layout.file_bytes = bytes;
  for (const auto& spec : tensors) {
    const auto start = header.Find(spec.name)->GetArray("data_offsets")
                           ->array[0].AsInt(-1);
    layout.tensors.push_back({spec.name, static_cast<size_t>(start),
                               layout.bytes, spec.bytes});
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
  const auto specs = codec ? TtsCodecDecoderTensorContracts()
                           : TtsTensorContracts(variant);
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
  if (layout.bytes != bytes ||
      layout.file_bytes != (codec ? TtsConfig::kCodecFileDataBytes : bytes))
    return Status::Fail("TTS weight storage differs");
  std::sort(intervals.begin(), intervals.end());
  size_t end = 0;
  for (const auto& interval : intervals) {
    if (interval.first < end || (!codec && interval.first != end))
      return Status::Fail("TTS weight overlap or gap");
    end = interval.second;
  }
  return {};
}

Status PlanTtsDenseWorkspace(TtsVariant variant, bool predictor,
                             size_t rows, size_t context, size_t max_bytes,
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
  const auto bf16 = [&](size_t n) { return carve(n * sizeof(uint16_t)); };
  w.hidden_state = bf16(rows * w.hidden);
  w.normalized = bf16(rows * w.hidden);
  w.projected = bf16(rows * w.hidden);
  w.q = bf16(rows * 2048);
  w.k = bf16(rows * 1024);
  w.v = bf16(rows * 1024);
  w.head_q = bf16(static_cast<size_t>(w.padded_rows) * 2048);
  w.rope = bf16(rows * 128);
  w.scores = bf16(rows * w.padded_context);
  w.head_output = bf16(rows * 128);
  w.attention = bf16(rows * 2048);
  w.gate = bf16(rows * w.intermediate);
  w.up = bf16(rows * w.intermediate);
  const size_t cache = static_cast<size_t>(w.layers) * w.padded_context * 1024;
  w.cache_k = bf16(cache);
  w.cache_v = bf16(cache);
  w.gemm = carve(TtsDenseLayout::kGemmBytes);
  if (w.bytes > max_bytes) return Status::Fail("TTS dense RAM budget exceeded");
  *out = w;
  return {};
}

Status PlanTtsGenerationWorkspace(TtsVariant variant, size_t text_tokens,
                                  size_t prefill_tokens, size_t max_frames,
                                  size_t max_bytes,
                                  TtsGenerationLayout* out) {
  if (!out || text_tokens < 4 || text_tokens > 32768 ||
      max_frames < 2 || max_frames > 4096 || prefill_tokens < 8 ||
      prefill_tokens > 32768 - max_frames)
    return Status::Fail("TTS text, prompt or generation budget invalid");
  TtsGenerationLayout w;
  if (auto s = PlanTtsDenseWorkspace(variant, false, prefill_tokens,
          prefill_tokens + max_frames, max_bytes, &w.talker); !s) return s;
  if (auto s = PlanTtsDenseWorkspace(variant, true, 2, 16,
                                     max_bytes, &w.predictor); !s) return s;
  const auto carve = [&](size_t bytes) {
    const size_t start = (w.bytes + 255) / 256 * 256;
    w.bytes = start + bytes;
    return start;
  };
  const auto bf16 = [&](size_t n) { return carve(n * sizeof(uint16_t)); };
  const size_t hidden = w.talker.hidden;
  w.talker_offset = carve(w.talker.bytes);
  w.predictor_offset = carve(w.predictor.bytes);
  w.ids = carve(std::max(text_tokens, size_t{16}) * sizeof(int32_t));
  w.text_lookup = bf16(text_tokens * 2048);
  w.text_intermediate = bf16(text_tokens * 2048);
  w.text_projected = bf16(text_tokens * hidden);
  w.affine_fp32 = carve(text_tokens * 2048 * sizeof(float));
  w.prefill = bf16(prefill_tokens * hidden);
  w.past_hidden = bf16(hidden);
  w.codec_embeddings = bf16(16 * hidden);
  w.decode_input = bf16(hidden);
  w.predictor_input = bf16(2 * hidden);
  w.predictor_projected = bf16(2 * 1024);
  w.logits = bf16(3072);
  if (w.bytes > max_bytes)
    return Status::Fail("TTS generation RAM budget exceeded");
  *out = w;
  return {};
}

}  // namespace q4t::audio
