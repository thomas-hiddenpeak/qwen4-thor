#pragma once

#include <string>
#include <cstdint>

namespace q4t::server {

struct AccessProgramPolicy;

// Describes this executable's protocol, not health or release acceptance.
std::string ChatCapabilitiesJson(const std::string& model, bool allow_media,
                                 bool mtp_enabled,
                                 uint64_t media_attention_shared_bytes = 0,
                                 const AccessProgramPolicy* access = nullptr,
                                 bool native_embedding = false,
                                 bool native_asr = false,
                                 bool native_aligner = false,
                                 bool native_vad = false,
                                 int vad_max_chunks = 64,
                                 bool native_speaker = false,
                                 bool native_separator = false);

}  // namespace q4t::server
