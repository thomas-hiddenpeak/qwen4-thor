// Native fixed SpeechBrain Fbank + sentence mean normalization, CPU only.
#pragma once

#include <cstddef>
#include <functional>
#include <span>
#include <vector>

#include "q4t/status.h"

namespace q4t::audio {
struct SpeakerFeatureLimits {
  size_t max_samples = 480000;
  size_t max_frames = 3001;
};
struct SpeakerFeatures {
  size_t frames = 0;
  // Contiguous float32 [80, frames]. Includes the final centered STFT frame.
  std::vector<float> mel;
};
// Exact 16 kHz mono input, no resampling. Atomic output on errors/cancellation.
// Original ASR features use a different trained contract and are unchanged.
Status ComputeSpeakerFeatures(std::span<const float> samples, int sample_rate,
                              const SpeakerFeatureLimits& limits,
                              SpeakerFeatures* out,
                              const std::function<bool()>& cancelled = {});
}  // namespace q4t::audio
