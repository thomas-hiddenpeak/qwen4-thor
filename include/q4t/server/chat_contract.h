#pragma once

#include <string>
#include <span>

#include "q4t/io/json.h"
#include "q4t/status.h"

namespace q4t::server {

// Validate transport options before registration, decoding media or GPU work.
Status ValidateChatContract(const io::Json& request,
                            const std::string& model_name, bool allow_media,
                            std::string* error_param = nullptr,
                            bool allow_empty_messages = false,
                            bool preparing_file_inputs = false,
                            std::span<const std::string> async_call_ids = {},
                            bool responses_tool_media = false);

}  // namespace q4t::server
