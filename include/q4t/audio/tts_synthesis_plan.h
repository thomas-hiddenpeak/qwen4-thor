// Exact chunk/crop planning for native reference-conditioned synthesis.
#pragma once

#include "q4t/audio/tts_codec_layout.h"

namespace q4t::audio {
struct TtsSynthesisChunk {
  bool ready = false;
  // Absolute coordinates in [reference frames, generated frames].
  size_t input_begin = 0, input_frames = 0;
  size_t discard_frames = 0, output_frames = 0;
};

// Retains the official global chunk grid and 25-frame replay context. Entire
// reference-only output chunks may be skipped; their independent computation
// cannot affect a later decoder call. No generated frames are discarded.
Status PlanTtsSynthesisChunk(size_t reference_frames, size_t generated_frames,
                             size_t emitted_frames, size_t chunk_frames,
                             bool ended, bool defer_until_eos,
                             TtsSynthesisChunk* out);
// Copies only the selected bounded input, including all16 groups. A failure
// leaves the previous owner unchanged; input spans may alias the old output.
Status BuildTtsSynthesisChunk(std::span<const TtsCodes> reference,
                              std::span<const TtsCodes> generated,
                              const TtsSynthesisChunk& chunk,
                              std::vector<TtsCodes>* out);
}  // namespace q4t::audio
