// CPU-only checked layouts for the native ASR backend.
#pragma once

#include <cstddef>
#include <string>
#include <vector>

#include "q4t/audio/asr_config.h"
#include "q4t/model/embedding_layout.h"

namespace q4t::audio {

struct AsrTensorExtent {
  std::string name;
  size_t offset = 0;
  size_t bytes = 0;
};

struct AsrWeightLayout {
  size_t bytes = 0;
  std::vector<AsrTensorExtent> tensors;
};

Status ParseAsrWeightLayout(const io::Json& header, size_t data_bytes,
                            AsrWeightLayout* out);
Status ValidateAsrWeightLayout(const AsrWeightLayout& layout);

struct AsrEncoderLayout {
  int frames = 0;
  int chunks = 0;
  int padded_frames = 0;
  int padded_tokens = 0;
  int tokens = 0;
  int attention_window = 0;
  size_t bytes = 0;
  size_t features = 0;
  size_t columns = 0;
  size_t convolution_a = 0;
  size_t convolution_b = 0;
  size_t conv_flat = 0;
  size_t chunk_hidden = 0;
  size_t hidden = 0;
  size_t normalized = 0;
  size_t q = 0;
  size_t k = 0;
  size_t v = 0;
  size_t attention = 0;
  size_t projected = 0;
  size_t intermediate = 0;
  size_t output = 0;
  size_t linear_fp32 = 0;
  size_t gemm = 0;
  static constexpr size_t kGemmBytes = 32 * 1024 * 1024;
};

// Workspace for one utterance; all chunks use the same padded convolution
// width, including the tail. No clipping. Explicit frame and byte budgets.
Status PlanAsrEncoderWorkspace(size_t frames, size_t max_frames,
                               size_t max_bytes, AsrEncoderLayout* out);

struct AsrDecoderLayout {
  model::EmbeddingWorkspaceLayout dense;
  size_t ids = 0;
  size_t input = 0;
  size_t last = 0;
  size_t logits = 0;
  size_t dense_offset = 0;
  size_t bytes = 0;
};

Status PlanAsrDecoderWorkspace(size_t tokens, size_t max_tokens,
                               size_t max_bytes, AsrDecoderLayout* out);

}  // namespace q4t::audio
