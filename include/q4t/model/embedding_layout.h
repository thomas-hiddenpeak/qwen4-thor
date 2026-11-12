// CPU-only, exact layouts for the pinned optional embedding backend.
#pragma once

#include <cstddef>
#include <string>
#include <vector>

#include "q4t/io/json.h"
#include "q4t/status.h"

namespace q4t::model {

struct EmbeddingTensorExtent {
  std::string name;
  size_t offset = 0;  // Relative to the safetensors data region.
  size_t bytes = 0;
};

struct EmbeddingWeightLayout {
  std::vector<EmbeddingTensorExtent> tensors;
  size_t bytes = 0;
};

// Exact tensor set, BF16 shape/extent, no overlaps/gaps/trailing payload.
// Parse the header with duplicate-key rejection before calling this method.
// Atomic output: a rejected header cannot replace a valid layout.
Status ParseEmbeddingWeightLayout(const io::Json& header, size_t data_bytes,
                                  EmbeddingWeightLayout* out);

struct EmbeddingWorkspaceLayout {
  int tokens = 0;
  int padded_tokens = 0;
  size_t bytes = 0;
  size_t ids = 0;
  size_t hidden = 0;
  size_t normalized = 0;
  size_t q = 0;
  size_t k = 0;
  size_t v = 0;
  size_t head_q = 0;
  size_t head_k = 0;
  size_t transposed_v = 0;
  size_t rope = 0;
  size_t scores = 0;
  size_t head_output = 0;
  size_t attention = 0;
  size_t projected = 0;
  size_t gate = 0;
  size_t up = 0;
  size_t gemm = 0;
  static constexpr size_t kGemmBytes = 32 * 1024 * 1024;
};

// One input at a time. Reuse one attention score matrix across all heads;
// every temporary, including cuBLAS scratch and token IDs, is accounted for.
Status PlanEmbeddingWorkspace(int tokens, EmbeddingWorkspaceLayout* out);

}  // namespace q4t::model
