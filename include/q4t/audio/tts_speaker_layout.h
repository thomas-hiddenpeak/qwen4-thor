// Native Base speaker storage. No file I/O, allocation of weights or CUDA.
#pragma once

#include "q4t/audio/tts_layout.h"

namespace q4t::audio {
struct TtsSpeakerConfig {
  static constexpr size_t kSourceBytes = 24002176;
  static constexpr size_t kBaseDataBytes = 3857354880;
  static constexpr size_t kDeviceBytes = kSourceBytes * 2;
  static constexpr size_t kMinFrames = 5, kMaxFrames = 5625;
  static constexpr int kMel = 128, kChannels = 512, kAggregate = 1536;
  static constexpr int kEmbedding = 2048;
};

// Parse the complete independently verified Base header, then select its 76
// speaker tensors. The complete source SHA must be verified before use, even
// when only this prefix is retained. Compact BF16 payload follows graph order.
Status ParseTtsSpeakerWeightLayout(const io::Json& header, size_t data_bytes,
                                   TtsWeightLayout* out);
Status ValidateTtsSpeakerWeightLayout(const TtsWeightLayout& layout);

struct TtsSpeakerResidualWeights {
  TtsAffineWeights before, after;
  std::array<TtsAffineWeights, 7> branches;
  TtsAffineWeights squeeze_first, squeeze_second;
};
struct TtsSpeakerDeviceWeights {
  TtsAffineWeights input;
  std::array<TtsSpeakerResidualWeights, 3> layers;
  TtsAffineWeights aggregate, attention, attention_output, output;
};

// Bind expanded FP32 storage; checkpoint expansion uses ExpandTtsWeightChunk.
// Publication is atomic; invalid extents and pointer overflow fail first.
Status BindTtsSpeakerDeviceWeights(const void* data, size_t bytes,
                                   const TtsWeightLayout& layout,
                                   TtsSpeakerDeviceWeights* out);

struct TtsSpeakerWorkspace {
  static constexpr size_t kGemmBytes = 4ULL << 20;
  int frames = 0;
  size_t mel = 0, hidden = 0, temporary = 0, res2 = 0, update = 0;
  size_t history = 0, aggregate = 0, context = 0, attention_hidden = 0;
  size_t attention = 0, columns = 0, pool = 0, squeeze = 0;
  size_t squeeze_hidden = 0, squeeze_gate = 0, output = 0;
  size_t gemm = 0, finite_flag = 0, bytes = 0;
};
Status PlanTtsSpeakerWorkspace(size_t frames, size_t max_frames,
                               size_t max_bytes, TtsSpeakerWorkspace* out);
}  // namespace q4t::audio
