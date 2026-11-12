#pragma once

#include <string>
#include <string_view>
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
  // Bound to historical call items, never inferred from today's tools.
  std::vector<std::string> async_tool_call_ids;
};

struct ResponsesToolIdentity {
  std::string type, name, name_space, model_name, path;
  bool async = false;
};
// Stable model names are independent of declaration order and current tools,
// so stored calls keep their identity even when a later turn changes tools.
std::string ResponsesToolModelName(std::string_view name_space,
                                   std::string_view name,
                                   std::string_view type);
Status PlanResponsesToolIdentities(
    const io::Json& request, std::vector<ResponsesToolIdentity>* identities,
    ChatPlanError* error);

// Object/file/encrypted references must be resolved by the owning request's
// client-scoped state layer first. This function never performs external I/O.
Status NormalizeResponsesInput(const io::Json& request,
                               ResponsesInputPlan* result, ChatPlanError* error,
                               bool allow_empty = false,
                               bool preparing_files = false);
// preparing_files validates and retains unresolved file/image parts for the
// preparation owner. Its output must not be sent to the model/template.
// Maps shared-generation errors back to the caller's Responses input path.
void ResponsesInputErrorParam(const ResponsesInputPlan& plan,
                              std::string* param);
Status NormalizeResponsesTools(const io::Json& request, io::Json* tools,
                               io::Json* choice, ChatPlanError* error,
                               io::Json* effective_tools = nullptr);
}  // namespace q4t::server
