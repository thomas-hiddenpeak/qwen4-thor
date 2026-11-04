// Qwen3.8-Flash-Next's model-specific chat template. Content is rendered once
// by the HTTP adapter so decoding image bytes never occurs during formatting.
#pragma once

#include <string>
#include <string_view>
#include <vector>

#include "q4t/io/json.h"
#include "q4t/status.h"

namespace q4t::server {

inline constexpr std::string_view kChatMessageProfile = "qwen3.8-native-v2";

// This model has one instruction slot. Developer is an alternative spelling
// for that leading system slot, not a second instruction hierarchy.
bool IsChatInstructionRole(std::string_view role);

// Shared by the HTTP contract and direct renderer. Reject roles, placement,
// names and instruction content that the native template cannot represent.
Status ValidateChatMessageProfile(const io::Json& messages,
                                  std::string* error_param = nullptr);

Status RenderChatPrompt(const io::Json& request,
                        const std::vector<std::string>& contents,
                        std::string* prompt);

}  // namespace q4t::server
