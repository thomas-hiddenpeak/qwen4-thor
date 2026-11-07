// Qwen3.8-Flash-Next's model-specific chat template. Content is rendered once
// by the HTTP adapter so decoding image bytes never occurs during formatting.
#pragma once

#include <string>
#include <string_view>
#include <vector>

#include "q4t/io/json.h"
#include "q4t/status.h"

namespace q4t::server {

inline constexpr std::string_view kChatMessageProfile = "qwen3.8-openai-v3";

// Both API instruction roles use the model's system ChatML role. Preserve
// message order; a lone unnamed leading instruction retains native bytes.
bool IsChatInstructionRole(std::string_view role);

// Shared by HTTP and the renderer: validate role-specific content, metadata
// and historical refusal without decoding media or changing the request.
Status ValidateChatMessageProfile(const io::Json& messages,
                                  std::string* error_param = nullptr);

Status RenderChatPrompt(const io::Json& request,
                        const std::vector<std::string>& contents,
                        std::string* prompt);

}  // namespace q4t::server
