// Qwen3.8-Flash-Next's model-specific chat template. Content is rendered once
// by the HTTP adapter so decoding image bytes never occurs during formatting.
#pragma once

#include <cstddef>
#include <span>
#include <string>
#include <string_view>
#include <vector>

#include "q4t/io/json.h"
#include "q4t/status.h"

namespace q4t::server {

inline constexpr std::string_view kChatMessageProfile = "qwen3.8-openai-v4";

// Both API instruction roles use the model's system ChatML role. Preserve
// message order. Parsed generation adds the native refusal adapter instruction.
bool IsChatInstructionRole(std::string_view role);

// Render the same model-native definitions at an ordered discovery point.
// No role framing is included; callers retain the actual item's position.
Status RenderChatToolDefinitions(const io::Json& tools, std::string* text);

// Actual content placement in the rendered prompt. Source coordinates refer
// to the transport-rendered content before Unicode trim. Assistant/tool
// framing and reordered tool replies are recorded without modifying bytes.
struct ChatMessageSpan {
  size_t source_begin = 0;
  size_t source_size = 0;
  size_t prompt_begin = 0;
  size_t prompt_end = 0;
};
// Internal invocation-time rendering policy. Later tool definitions must not
// change already rendered calls; this field is not accepted from public Chat.
struct ChatToolCallFormat {
  std::string call_id;
  bool json_arguments = false;
};

// Shared by HTTP and the renderer: validate role-specific content, metadata
// and historical refusal without decoding media or changing the request.
Status ValidateChatMessageProfile(const io::Json& messages,
                                  std::string* error_param = nullptr,
                                  bool allow_empty = false);

Status RenderChatPrompt(const io::Json& request,
                        const std::vector<std::string>& contents,
                        std::string* prompt,
                        std::vector<ChatMessageSpan>* spans = nullptr,
                        bool allow_empty = false,
                        std::span<const std::string> async_call_ids = {},
                        std::span<const io::Json> historical_tools = {},
                        const io::Json* header_tools = nullptr,
                        std::span<const ChatToolCallFormat> call_formats = {});

}  // namespace q4t::server
