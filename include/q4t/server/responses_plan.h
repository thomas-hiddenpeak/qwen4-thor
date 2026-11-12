#pragma once

#include <functional>
#include <string>
#include <vector>

#include "q4t/io/json.h"
#include "q4t/server/chat_plan.h"
#include "q4t/server/moderation.h"
#include "q4t/server/responses_input.h"
#include "q4t/status.h"

namespace q4t::server {
// The envelope is validated before state/file/tool I/O. Lifecycle fields are
// execution requirements, not ignored Chat metadata. The owning service must
// resolve them before preparing and publishing a generated response.
struct ResponsesEnvelope {
  io::Json request;
  io::Json options;
  PromptCacheOptions cache;
  ModerationOptions moderation;
  AccessProgramSelection access_program;
  bool stream = false;
  bool store = true;
  bool background = false;
  bool obfuscation = true;
  bool logprobs = false;
  bool truncate = false;
  bool encrypted_reasoning = false;
  // Execution-owned budget policy, never accepted from public JSON.
  bool native_tools_exhausted = false;
  std::string previous_response_id;
  std::string conversation_id;
  std::string summary;
  std::string reasoning_context = "all_turns";
  std::string reasoning_effort = "xhigh";
  std::string reasoning_mode = "standard";
  std::string verbosity = "medium";
  std::string personality;
};

Status ParseResponsesEnvelope(const io::Json& request,
                              const ChatPlanConfig& config,
                              ResponsesEnvelope* envelope,
                              ChatPlanError* error);
// Compact has its own public request shape; never accept create-only options
// and silently ignore them. It remains stateless and uses the server budget.
Status NormalizeResponsesCompactRequest(const io::Json& request,
                                        const ChatPlanConfig& config,
                                        io::Json* normalized,
                                        ChatPlanError* error);

struct ResponsesGenerationPlan {
  ResponsesInputPlan input;
  io::Json generation_request;
  io::Json effective_tools;
  io::Json active_tools;
  ChatRequestPlan chat;
  size_t truncated_items = 0;
  std::vector<size_t> input_source_indices;
};

struct ResponsesSummaryPlan {
  bool needed = false;
  bool budget_exhausted = false;
  io::Json request;
  ChatRequestPlan chat;
};

// Prepare a distinct model step from actual generated reasoning, without
// changing original inputs or silently replenishing the output budget.
Status PlanResponsesSummary(const ResponsesEnvelope& envelope,
                            const io::Json& snapshot, int output_limit,
                            int64_t output_used, const ChatPlanConfig& config,
                            ResponsesSummaryPlan* plan, ChatPlanError* error);

// Measures the actual model template, tokenizer and expanded media without
// generating tokens or mutating stored history. Only context overflow permits
// truncation; credentials, malformed input and client quotas are not bypassed.
using ResponsesContextCheck =
    std::function<Status(const ResponsesGenerationPlan&, ChatPlanError*)>;
Status FitResponsesContext(const ResponsesEnvelope& envelope,
                           const ChatPlanConfig& config,
                           const ResponsesContextCheck& check,
                           ResponsesGenerationPlan* plan, ChatPlanError* error);

// Pure validation before file/network/codec I/O. Retains every actual content
// part for semantic/history checks; does not invent a generation prompt.
Status ValidateResponsesPreparation(const ResponsesEnvelope& envelope,
                                    const io::Json& resolved_input,
                                    const ChatPlanConfig& config,
                                    io::Json* effective_tools,
                                    ChatPlanError* error);

// resolved_input includes the authorized previous/conversation history and
// resolved media. No I/O here. Instructions always belong to the new request.
Status PrepareResponsesGeneration(const ResponsesEnvelope& envelope,
                                  const io::Json& resolved_input,
                                  const ChatPlanConfig& config,
                                  ResponsesGenerationPlan* plan,
                                  ChatPlanError* error);
}  // namespace q4t::server
