// Actual bounded realtime audio bytes -> the native ASR's normalized PCM.
#pragma once

#include <functional>
#include <vector>

#include "q4t/server/realtime_audio_queue.h"

namespace q4t::server {
// The committed queue spans remain borrowed until the caller's worker has
// stopped. No container codec, external process, padded output or timestamp
// rounding. Output is transactional, including cancellation and invalid spans.
Status DecodeRealtimePcm(const RealtimeAudioItem& item,
                         const std::array<std::span<const uint8_t>, 2>& bytes,
                         size_t max_output_samples, std::vector<float>* output,
                         const std::function<bool()>& cancelled = {});
}  // namespace q4t::server
