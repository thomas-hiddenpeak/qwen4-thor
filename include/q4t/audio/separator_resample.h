// Bounded, phase-preserving 16k/8k conversion for the native separator.
#pragma once

#include <cstddef>
#include <cstdint>
#include <functional>
#include <span>
#include <vector>

#include "q4t/status.h"

namespace q4t::audio {
inline constexpr size_t kSeparationMaxInputSamples = 480000;
// Fixed 65-tap Hann-windowed sinc, 3600 Hz cutoff at 16 kHz, unity DC.
// Zero extension and centered filtering preserve the original time origin.
// Decimation takes even samples; restoration uses the caller's exact original
// length. Output is atomic on invalid input, capacity failure or cancellation.
Status DownsampleSeparation(std::span<const float> input,
                            size_t max_input_samples,
                            std::vector<float>* output,
                            const std::function<bool()>& cancelled = {});
Status RestoreSeparation(std::span<const float> input, size_t original_samples,
                         size_t max_output_samples, std::vector<float>* output,
                         const std::function<bool()>& cancelled = {});
}  // namespace q4t::audio
