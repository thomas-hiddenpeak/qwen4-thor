// CPU-only validated weights and caller-owned workspace for native SepFormer.
#pragma once

#include "q4t/audio/separator_config.h"

namespace q4t::audio {
struct SeparatorAffineWeights {
  const float* weight = nullptr;
  const float* bias = nullptr;
};
struct SeparatorLayerWeights {
  SeparatorAffineWeights norm1, norm2, qkv, attention, ffn1, ffn2;
};
struct SeparatorPathWeights {
  const float* position = nullptr;
  std::array<SeparatorLayerWeights, SeparatorConfig::kLayers> layers;
  SeparatorAffineWeights norm, group_norm;
};
struct SeparatorDeviceWeights {
  const float* encoder = nullptr;
  const float* decoder = nullptr;
  const float* project = nullptr;
  const float* prelu = nullptr;
  const float* mask_project = nullptr;
  SeparatorAffineWeights norm, sources, output, gate;
  // Block 0 intra/inter, then block 1 intra/inter.
  std::array<SeparatorPathWeights, 4> paths;
};
Status BindSeparatorDeviceWeights(const void* data, size_t bytes,
                                  const SeparatorTensorLayout& layout,
                                  SeparatorDeviceWeights* out);

struct SeparatorWorkspaceLayout {
  static constexpr size_t kMaxSamples = 240000;
  static constexpr size_t kGemmBytes = 4ULL << 20;
  int samples = 0, frames = 0, gap = 0, segments = 0, rows = 0;
  int decoded_samples = 0, reduction_blocks = 0;
  size_t pcm = 0, encoded = 0, normalized = 0, projected = 0;
  size_t hidden = 0, path = 0, norm = 0, qkv = 0;
  size_t scores = 0, attention = 0, ffn = 0, update = 0;
  size_t source_chunks = 0, joined = 0, output_hidden = 0, gate = 0;
  size_t masks = 0, separated = 0, partial = 0, statistics = 0;
  size_t gemm = 0, finite_flag = 0, bytes = 0;
};
Status PlanSeparatorWorkspace(size_t samples, size_t max_samples,
                              size_t max_bytes, SeparatorWorkspaceLayout* out);
}  // namespace q4t::audio
