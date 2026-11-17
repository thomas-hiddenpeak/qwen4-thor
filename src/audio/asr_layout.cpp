#include "q4t/audio/asr_layout.h"

#include <algorithm>
#include <limits>
#include <utility>

#include "q4t/model/embedding_config.h"
#include "asr_layout_internal.h"

namespace q4t::audio {

Status ParseAsrWeightLayout(const io::Json& header, size_t data_bytes,
                            AsrWeightLayout* out) {
  if (auto s = ValidateAsrTensorHeader(header, data_bytes); !s) return s;
  return detail::ParseSpeechWeightLayout(header, data_bytes,
                                         AsrTensorContracts(), out);
}

Status ValidateAsrWeightLayout(const AsrWeightLayout& layout) {
  return detail::ValidateSpeechWeightLayout(layout, AsrTensorContracts(),
                                            AsrWeightBytes());
}

Status PlanAsrEncoderWorkspace(size_t frames, size_t max_frames,
                               size_t max_bytes, AsrEncoderLayout* out) {
  return detail::PlanSpeechEncoderWorkspace<AsrConfig>(frames, max_frames,
                                                       max_bytes, out);
}

Status PlanAsrDecoderWorkspace(size_t tokens, size_t max_tokens,
                               size_t max_bytes, AsrDecoderLayout* out) {
  if (!out || tokens == 0 || tokens > max_tokens ||
      tokens > model::EmbeddingConfig::kInputTokens)
    return Status::Fail("ASR decoder token budget exceeded");
  AsrDecoderLayout w;
  if (auto s =
          model::PlanEmbeddingWorkspace(static_cast<int>(tokens), &w.dense);
      !s)
    return s;
  const auto carve = [&](size_t bytes) {
    const size_t start = (w.bytes + 255) / 256 * 256;
    w.bytes = start + bytes;
    return start;
  };
  w.ids = carve(tokens * sizeof(int32_t));
  w.input = carve(tokens * AsrConfig::kHidden * sizeof(uint16_t));
  w.last = carve(AsrConfig::kHidden * sizeof(uint16_t));
  w.logits = carve(AsrConfig::kVocab * sizeof(uint16_t));
  w.dense_offset = carve(w.dense.bytes);
  if (w.bytes > max_bytes)
    return Status::Fail("ASR decoder workspace budget exceeded");
  *out = w;
  return {};
}

}  // namespace q4t::audio
