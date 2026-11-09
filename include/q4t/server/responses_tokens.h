#pragma once

#include "q4t/server/responses_state.h"

namespace q4t::server {

// Strict count-only contract. Accepts an omitted/null body, resolves only
// authenticated existing state, and never begins a response or runs a tool.
// Callers use config.count_input_only for the shared template/token budget.
ObjectReply PrepareResponsesTokenCount(const io::Json& request,
                                       const ChatPlanConfig& config,
                                       ObjectStore& objects,
                                       const StateAccess& access,
                                       ResponsesEnvelope* envelope,
                                       ResponsesGenerationPlan* generation);

// Enforces the request deadline even when no storage read is necessary.
ObjectReply CheckResponsesTokenCountActive(const StateAccess& access);
io::Json ResponsesTokenCountJson(size_t tokens);

}  // namespace q4t::server
