// Native Qwen3-TTS Base speaker features. No model weights or CUDA dependency.
#pragma once

#include <cstddef>
#include <functional>
#include <span>
#include <vector>

#include "q4t/status.h"

namespace q4t::audio {
struct TtsSpeakerFeatureLimits {
  size_t max_samples = 1440000;  // 60 seconds at 24 kHz.
  size_t max_frames = 5625;
  // Includes caller input, output and fixed tables/scratch conservatively.
  size_t max_bytes = 32ULL << 20;
};
struct TtsSpeakerFeatures {
  size_t frames = 0;
  // Float32 [128, frames], natural log of Slaney-filtered magnitudes.
  std::vector<float> mel;
};

// Count fixed table/scratch residency even though it is shared between calls.
size_t TtsSpeakerFeatureFixedBytes();

// Exact 24 kHz mono input. No resampling, cropping, sentence mean subtraction
// or ASR normalization. Shorter-than-reflection inputs fail atomically.
// Cancellation is checked before initialization, per frame and at publication.
Status ComputeTtsSpeakerFeatures(std::span<const float> samples,
                                 int sample_rate,
                                 const TtsSpeakerFeatureLimits& limits,
                                 TtsSpeakerFeatures* out,
                                 const std::function<bool()>& cancelled = {});
}  // namespace q4t::audio
