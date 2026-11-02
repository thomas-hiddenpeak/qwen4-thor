#pragma once

#include <string>

#include "q4t/io/json.h"
#include "q4t/status.h"

namespace q4t::server {

// Validate transport options before registration, decoding media or GPU work.
Status ValidateChatContract(const io::Json& request,
                            const std::string& model_name, bool allow_media,
                            std::string* error_param = nullptr);

}  // namespace q4t::server
