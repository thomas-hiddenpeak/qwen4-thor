#include "q4t/audio/tts_synthesis_plan.h"

#include <algorithm>

namespace q4t::audio {
Status PlanTtsSynthesisChunk(size_t reference_frames, size_t generated_frames,
                             size_t emitted_frames, size_t chunk_frames,
                             bool ended, bool defer_until_eos,
                             TtsSynthesisChunk* out) {
  if (!out || reference_frames > TtsPrompt::kMaxReferenceFrames ||
      generated_frames > 4096 || emitted_frames > generated_frames ||
      !chunk_frames || chunk_frames > TtsCodecConfig::kChunkFrames)
    return Status::Fail("TTS reference synthesis chunk bounds invalid");
  const size_t total = reference_frames + generated_frames;
  const size_t position = reference_frames + emitted_frames;
  if (emitted_frames && emitted_frames < generated_frames &&
      position % chunk_frames)
    return Status::Fail("TTS synthesis output is not at a codec boundary");
  TtsSynthesisChunk chunk;
  if (emitted_frames == generated_frames || (defer_until_eos && !ended)) {
    *out = chunk;
    return {};
  }
  const size_t begin = position / chunk_frames * chunk_frames;
  const size_t end = std::min(begin + chunk_frames, total);
  if (!ended && end < begin + chunk_frames) {
    *out = chunk;
    return {};
  }
  chunk.ready = true;
  chunk.input_begin = begin - std::min(begin, TtsCodecConfig::kLeftContext);
  chunk.input_frames = end - chunk.input_begin;
  chunk.discard_frames = position - chunk.input_begin;
  chunk.output_frames = end - position;
  *out = chunk;
  return {};
}

Status BuildTtsSynthesisChunk(std::span<const TtsCodes> reference,
                              std::span<const TtsCodes> generated,
                              const TtsSynthesisChunk& chunk,
                              std::vector<TtsCodes>* out) {
  if (!out || reference.size() > TtsPrompt::kMaxReferenceFrames ||
      generated.size() > 4096 || !chunk.ready || !chunk.input_frames ||
      chunk.input_frames > TtsCodecConfig::kMaxInputFrames ||
      chunk.input_begin > reference.size() + generated.size() ||
      chunk.input_frames >
          reference.size() + generated.size() - chunk.input_begin ||
      chunk.discard_frames >= chunk.input_frames ||
      chunk.output_frames != chunk.input_frames - chunk.discard_frames ||
      chunk.input_begin + chunk.discard_frames < reference.size())
    return Status::Fail("TTS reference synthesis input slice invalid");
  std::vector<TtsCodes> codes;
  codes.reserve(chunk.input_frames);
  for (size_t i = 0; i < chunk.input_frames; ++i) {
    const size_t position = chunk.input_begin + i;
    codes.push_back(position < reference.size()
                        ? reference[position]
                        : generated[position - reference.size()]);
  }
  if (auto status = ValidateTtsCodecCodes(codes); !status) return status;
  *out = std::move(codes);
  return {};
}
}  // namespace q4t::audio
