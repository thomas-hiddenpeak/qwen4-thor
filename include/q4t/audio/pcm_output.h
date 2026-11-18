// Native 24 kHz mono PCM16 / canonical finite RIFF-WAVE output.
#pragma once

#include <span>
#include <string>

#include "q4t/status.h"

namespace q4t::audio {
// Atomic output: no partial bytes are published on invalid PCM or budget
// failure. Compressed formats use the separate isolated codec worker.
Status EncodeSpeechPcm(std::span<const float> pcm, bool wave,
                       size_t max_output_bytes, std::string* out);
}  // namespace q4t::audio
