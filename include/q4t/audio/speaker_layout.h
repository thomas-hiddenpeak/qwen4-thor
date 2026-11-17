// Checked CPU-only weights/workspace binding for fixed ECAPA inference.
#pragma once

#include "q4t/audio/speaker_config.h"

namespace q4t::audio {
struct SpeakerAffineWeights {
  const float* weight = nullptr;
  const float* bias = nullptr;
};
struct SpeakerNormWeights {
  const float* weight = nullptr;
  const float* bias = nullptr;
  const float* mean = nullptr;
  const float* variance = nullptr;
};
struct SpeakerTdnnWeights {
  SpeakerAffineWeights affine;
  SpeakerNormWeights norm;
};
struct SpeakerRes2Weights {
  SpeakerTdnnWeights before, after;
  std::array<SpeakerTdnnWeights, 7> branches;
  SpeakerAffineWeights se_first, se_second;
};
struct SpeakerDeviceWeights {
  SpeakerTdnnWeights input;
  std::array<SpeakerRes2Weights, 3> layers;
  SpeakerTdnnWeights aggregate, attention;
  SpeakerAffineWeights attention_output, output;
  SpeakerNormWeights pool_norm;
};
Status BindSpeakerDeviceWeights(const void* data, size_t bytes,
                                const SpeakerTensorLayout& layout,
                                SpeakerDeviceWeights* out);

struct SpeakerWorkspaceLayout {
  static constexpr size_t kMaxFrames = 3001;
  static constexpr size_t kGemmBytes = 4ULL << 20;
  int frames = 0;
  size_t mel = 0, hidden = 0, temporary = 0, res2 = 0, update = 0;
  size_t history = 0, aggregate = 0, context = 0, attention_hidden = 0;
  size_t attention = 0, columns = 0, pool = 0, squeeze = 0;
  size_t squeeze_hidden = 0, squeeze_gate = 0, output = 0;
  size_t gemm = 0, finite_flag = 0, bytes = 0;
};
Status PlanSpeakerWorkspace(size_t frames, size_t max_frames, size_t max_bytes,
                            SpeakerWorkspaceLayout* out);
}  // namespace q4t::audio
