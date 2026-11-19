// Export the actual processed waveform; no model, codec process or asset I/O.
#pragma once

#include <functional>
#include <string>

#include "q4t/server/realtime_audio_queue.h"

namespace q4t::server {
// G.711 companding of a signed linear sample (mu-law when true, A-law when
// false). Separate from sample-rate conversion and floating-point saturation.
uint8_t EncodeRealtimeG711(int16_t sample, bool mu_law);

// Actual mono 16 kHz processed samples -> session PCM16/24k or G.711/8k.
// Integer sample counting, no padding, fixed resampling scratch; output is
// transactional on cancellation, invalid input or budget exhaustion. Finite
// filter overshoot is preserved through resampling and saturated only at final
// S16 quantization. Source samples are never changed. max_bytes bounds encoded
// binary data; the caller separately reserves Base64/JSON/frame ownership.
Status EncodeRealtimeAudio(std::span<const float> samples,
                            RealtimeAudioFormat format, size_t max_bytes,
                            std::string* output,
                            const std::function<bool()>& cancelled = {});
}  // namespace q4t::server
