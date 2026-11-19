// Copy verified assets into caller-owned CUDA storage, with bounded steps.
#pragma once

#include <functional>

#include "q4t/audio/tts_codec_layout.h"
#include "q4t/audio/tts_model.h"
#include "q4t/server/tts_assets.h"

namespace q4t::server {
struct TtsBoundWeights {
  audio::TtsDeviceWeights talker;
  audio::TtsCodecDeviceWeights codec;
};
// Caller holds shared GPU execution ownership and owns/free()s both device
// buffers, including on cancellation/failure. Only success publishes bindings.
// At most 2 MiB source + 4 MiB expansion scratch; no full F32 host duplicate.
// Returns with the stream drained. cancelled is checked before every chunk.
audio::TtsComputeResult UploadTtsWeights(
    const TtsHostAssets& assets, std::span<uint8_t> talker_device,
    std::span<uint8_t> codec_device, TtsBoundWeights* out,
    const std::function<bool()>& cancelled = {}, cudaStream_t stream = nullptr);
// Uses only the independently trained Base speaker subgraph. A failed upload
// never publishes a vector-producing graph; the caller owns the raw buffer.
audio::TtsComputeResult UploadTtsSpeakerWeights(
    const TtsHostAssets& assets, std::span<uint8_t> speaker_device,
    audio::TtsSpeakerDeviceWeights* out,
    const std::function<bool()>& cancelled = {}, cudaStream_t stream = nullptr);
// One allocation owns both registration graphs; subranges never overlap.
struct TtsVoiceBoundWeights {
  audio::TtsSpeakerDeviceWeights speaker;
  audio::TtsEncoderDeviceWeights encoder;
};
audio::TtsComputeResult UploadTtsVoiceWeights(
    const TtsHostAssets& assets, std::span<uint8_t> device,
    TtsVoiceBoundWeights* out, const std::function<bool()>& cancelled = {},
    cudaStream_t stream = nullptr);
}  // namespace q4t::server
