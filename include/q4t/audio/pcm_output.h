// Native 24 kHz mono PCM16 / canonical finite RIFF-WAVE output.
#pragma once

#include <cstdint>
#include <span>
#include <string>

#include "q4t/status.h"

namespace q4t::audio {
// Canonical little-endian signed 16-bit PCM. Empty finite recordings are valid.
Status PcmWaveHeader(uint32_t rate, uint16_t channels, size_t frames,
                     size_t max_output_bytes, std::string* out);
// Known final sample count is mandatory for RIFF; does not invent a length
// for an unfinished synthesis. The later body must match this exact count.
Status SpeechWaveHeader(size_t samples, size_t max_output_bytes,
                        std::string* out);
// Atomic output: no partial bytes are published on invalid PCM or budget
// failure. Compressed formats use the separate isolated codec worker.
Status EncodeSpeechPcm(std::span<const float> pcm, bool wave,
                       size_t max_output_bytes, std::string* out);
}  // namespace q4t::audio
