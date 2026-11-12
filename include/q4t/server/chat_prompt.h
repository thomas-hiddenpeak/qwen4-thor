#pragma once

#include <string>
#include <vector>

#include "q4t/io/json.h"
#include "q4t/server/output_constraints.h"
#include "q4t/server/chat_template.h"
#include "q4t/status.h"
#include "q4t/vision/processor.h"

namespace q4t::server {

// Transport-decoded media, in template order. No model or CUDA ownership.
struct VisionItem {
  enum Kind { kImage, kVideo };
  Kind kind = kImage;
  std::vector<std::string> frames;
  size_t message_index = 0;
  size_t part_index = 0;
  vision::ImageDetail detail = vision::ImageDetail::kAuto;
};

struct ChatPromptLayout {
  std::vector<ChatMessageSpan> messages;
  // Per-message source-byte ends of the rendered content parts, including
  // visual placeholders. A string content has one entry.
  std::vector<std::vector<size_t>> part_ends;
};

// Shared by generation and preflight after ValidateChatContract. The same
// constraint adapter and renderer determine the actual generation prefix.
Status PrepareChatPrompt(const io::Json& request,
                         const OutputConstraints& constraints,
                         std::string* prompt, std::vector<VisionItem>* items,
                         std::string* error_param,
                         std::string* error_code = nullptr,
                         ChatPromptLayout* layout = nullptr,
                         bool allow_empty = false,
                         std::span<const std::string> async_call_ids = {},
                         bool responses_tool_media = false);

}  // namespace q4t::server
