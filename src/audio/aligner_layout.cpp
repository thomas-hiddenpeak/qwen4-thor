#include "q4t/audio/aligner_layout.h"

#include "asr_layout_internal.h"

namespace q4t::audio {

Status PlanAlignerSequence(std::span<const int32_t> tokens, size_t audio_tokens,
                           std::vector<int32_t>* positions) {
  using C = AlignerConfig;
  if (!positions || tokens.size() > C::kContext || tokens.size() < 6 ||
      !audio_tokens || audio_tokens > tokens.size() - 5 ||
      tokens.front() != C::kAudioStart ||
      tokens[audio_tokens + 1] != C::kAudioEnd)
    return Status::Fail("alignment sequence/audio prefix differs");
  for (size_t i = 1; i <= audio_tokens; ++i)
    if (tokens[i] != C::kAudioPad)
      return Status::Fail("alignment audio position differs");
  std::vector<int32_t> slots;
  bool word = false;
  for (size_t i = audio_tokens + 2; i < tokens.size(); ++i) {
    if (tokens[i] == C::kTimestampToken) {
      if (!word || i + 1 >= tokens.size() ||
          tokens[i + 1] != C::kTimestampToken)
        return Status::Fail("alignment timestamp pair differs");
      slots.push_back(static_cast<int32_t>(i));
      slots.push_back(static_cast<int32_t>(++i));
      word = false;
    } else {
      if (tokens[i] < 0 || tokens[i] >= C::kEos)
        return Status::Fail("alignment word contains a nonliteral marker");
      word = true;
    }
  }
  if (slots.empty() || word)
    return Status::Fail("alignment sequence lacks terminal time pair");
  *positions = std::move(slots);
  return {};
}

Status ParseAlignerWeightLayout(const io::Json& header, size_t data_bytes,
                                AsrWeightLayout* out) {
  if (auto s = ValidateAlignerTensorHeader(header, data_bytes); !s) return s;
  return detail::ParseSpeechWeightLayout(header, data_bytes,
                                         AlignerTensorContracts(), out);
}

Status ValidateAlignerWeightLayout(const AsrWeightLayout& layout) {
  return detail::ValidateSpeechWeightLayout(layout, AlignerTensorContracts(),
                                            AlignerWeightBytes());
}

Status PlanAlignerEncoderWorkspace(size_t frames, size_t max_frames,
                                   size_t max_bytes, AsrEncoderLayout* out) {
  return detail::PlanSpeechEncoderWorkspace<AlignerConfig>(frames, max_frames,
                                                           max_bytes, out);
}

Status PlanAlignerDecoderWorkspace(size_t tokens, size_t slots,
                                   size_t max_tokens, size_t max_bytes,
                                   AlignerDecoderLayout* out) {
  using C = AlignerConfig;
  if (!out || !tokens || tokens > max_tokens || tokens > C::kContext ||
      !slots || slots % 2 || slots > tokens)
    return Status::Fail("alignment context or timestamp slot budget exceeded");
  AlignerDecoderLayout w;
  if (auto s =
          model::PlanEmbeddingWorkspace(static_cast<int>(tokens), &w.dense);
      !s)
    return s;
  w.slots = static_cast<int>(slots);
  // tokens/slots <= 8192, so all products fit even a 32-bit size_t.
  const auto carve = [&](size_t bytes) {
    const size_t start = (w.bytes + 255) / 256 * 256;
    w.bytes = start + bytes;
    return start;
  };
  w.ids = carve(tokens * sizeof(int32_t));
  w.input = carve(tokens * C::kHidden * sizeof(uint16_t));
  w.positions = carve(slots * sizeof(int32_t));
  w.selected = carve(slots * C::kHidden * sizeof(uint16_t));
  w.logits = carve(slots * C::kClasses * sizeof(uint16_t));
  w.dense_offset = carve(w.dense.bytes);
  if (w.bytes > max_bytes)
    return Status::Fail("alignment decoder workspace budget exceeded");
  *out = w;
  return {};
}

}  // namespace q4t::audio
