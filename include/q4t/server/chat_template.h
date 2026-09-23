// Qwen3.8-Flash-Next's model-specific chat template. Content is rendered once
// by the HTTP adapter so decoding image bytes never occurs during formatting.
#pragma once

#include <string>
#include <vector>

#include "q4t/io/json.h"
#include "q4t/status.h"

namespace q4t::server {

Status RenderChatPrompt(const io::Json& request,
                        const std::vector<std::string>& contents,
                        std::string* prompt);

}  // namespace q4t::server
