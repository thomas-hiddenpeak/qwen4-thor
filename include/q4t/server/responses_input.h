#pragma once

#include <string>
#include <vector>

#include "q4t/io/json.h"
#include "q4t/server/chat_plan.h"
#include "q4t/server/prompt_cache_policy.h"
#include "q4t/status.h"

namespace q4t::server {
struct ResponsesInputPlan {
  io::Json items;     // Original canonical items, including metadata for A08.
  io::Json messages;  // Native model adapter; no internal HTTP request.
  std::vector<std::string> message_paths;
  std::vector<PromptCacheInputBoundary> cache_boundaries;
};

// Object/file/encrypted references must be resolved by the owning request's
// client-scoped state layer first. This function never performs external I/O.
Status NormalizeResponsesInput(const io::Json& request,
                               ResponsesInputPlan* result,
                               ChatPlanError* error);
// Maps shared-generation errors back to the caller's Responses input path.
void ResponsesInputErrorParam(const ResponsesInputPlan& plan,
                              std::string* param);
Status NormalizeResponsesTools(const io::Json& request, io::Json* tools,
                               io::Json* choice, ChatPlanError* error,
                               io::Json* effective_tools = nullptr);
}  // namespace q4t::server
