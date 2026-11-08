#pragma once

#include <string>
#include <vector>

#include "q4t/io/json.h"
#include "q4t/server/chat_plan.h"
#include "q4t/server/responses_input.h"
#include "q4t/status.h"

namespace q4t::server {
// The envelope is validated before state/file/tool I/O. Lifecycle fields are
// execution requirements, not ignored Chat metadata. The owning service must
// resolve them before preparing and publishing a generated response.
struct ResponsesEnvelope {
  io::Json request;
  io::Json options;
  bool stream = false;
  bool store = true;
  bool background = false;
  bool obfuscation = true;
  bool logprobs = false;
  bool truncate = false;
  bool encrypted_reasoning = false;
  std::string previous_response_id;
  std::string conversation_id;
  std::string summary;
  std::string reasoning_context = "all_turns";
  std::string reasoning_effort = "xhigh";
  std::string reasoning_mode = "standard";
  std::string verbosity = "medium";
};

Status ParseResponsesEnvelope(const io::Json& request,
                              const ChatPlanConfig& config,
                              ResponsesEnvelope* envelope,
                              ChatPlanError* error);

struct ResponsesGenerationPlan {
  ResponsesInputPlan input;
  io::Json generation_request;
  io::Json effective_tools;
  ChatRequestPlan chat;
};

// resolved_input includes the authorized previous/conversation history and
// resolved media. No I/O here. Instructions always belong to the new request.
Status PrepareResponsesGeneration(const ResponsesEnvelope& envelope,
                                  const io::Json& resolved_input,
                                  const ChatPlanConfig& config,
                                  ResponsesGenerationPlan* plan,
                                  ChatPlanError* error);
}  // namespace q4t::server
