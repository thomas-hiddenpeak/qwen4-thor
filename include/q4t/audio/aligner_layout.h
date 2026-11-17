// Checked layouts for the fixed native forced-alignment model. No CUDA here.
#pragma once

#include <span>

#include "q4t/audio/aligner_config.h"
#include "q4t/audio/asr_layout.h"

namespace q4t::audio {

Status ParseAlignerWeightLayout(const io::Json& header, size_t data_bytes,
                                AsrWeightLayout* out);
Status ValidateAlignerWeightLayout(const AsrWeightLayout& layout);
Status PlanAlignerEncoderWorkspace(size_t frames, size_t max_frames,
                                   size_t max_bytes, AsrEncoderLayout* out);

struct AlignerDecoderLayout {
  model::EmbeddingWorkspaceLayout dense;
  int slots = 0;
  size_t ids = 0;
  size_t input = 0;
  size_t positions = 0;
  size_t selected = 0;
  size_t logits = 0;
  size_t dense_offset = 0;
  size_t bytes = 0;
};

// Two timestamp slots per word, within the actual 8192-position context.
// No truncation and no assumption that only the last row needs a head.
Status PlanAlignerDecoderWorkspace(size_t tokens, size_t slots,
                                   size_t max_tokens, size_t max_bytes,
                                   AlignerDecoderLayout* out);

// Exact audio prefix followed by literal word IDs and two slots per word.
// Returns strictly increasing real token positions; output is atomic.
Status PlanAlignerSequence(std::span<const int32_t> tokens, size_t audio_tokens,
                           std::vector<int32_t>* positions);

}  // namespace q4t::audio
