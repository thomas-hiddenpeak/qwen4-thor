// Bounded native log-mel features for the pinned Qwen3-ASR audio encoder.
#pragma once

#include <cstddef>
#include <functional>
#include <span>
#include <vector>

#include "q4t/status.h"

namespace q4t::audio {

struct AsrFeatureLimits {
  size_t max_samples = 480000;
  size_t max_frames = 3000;
};

struct AsrFeatures {
  size_t frames = 0;
  // Float32, contiguous [128, frames]; no channel mixing or resampling here.
  std::vector<float> mel;
};

// Input is mono PCM at 16 kHz. Limits and cancellation are supplied by the
// request owner. Validates before allocating; any failure preserves *out.
// No CUDA initialization, codec, external process, or runtime Python dependency.
Status ComputeAsrFeatures(std::span<const float> samples, int sample_rate,
                          const AsrFeatureLimits& limits, AsrFeatures* out,
                          const std::function<bool()>& cancelled = {});

}  // namespace q4t::audio
