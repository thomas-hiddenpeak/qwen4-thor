#pragma once

#include <string>
#include <cstdint>

namespace q4t::server {

struct AccessProgramPolicy;

// Describes this executable's protocol, not health or release acceptance.
std::string ChatCapabilitiesJson(const std::string& model, bool allow_media,
                                 bool mtp_enabled,
                                 uint64_t media_attention_shared_bytes = 0,
                                 const AccessProgramPolicy* access = nullptr);

}  // namespace q4t::server
