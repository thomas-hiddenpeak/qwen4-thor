#pragma once

#include <string>

namespace q4t::server {

// Describes this executable's protocol, not health or release acceptance.
std::string ChatCapabilitiesJson(const std::string& model, bool allow_media,
                                 bool mtp_enabled);

}  // namespace q4t::server
