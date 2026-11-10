#pragma once

#include "q4t/server/responses_state.h"
#include "q4t/server/file_expansion.h"

namespace q4t::server {

// Strict count-only contract. Accepts an omitted/null body, resolves only
// authenticated existing state, and never begins a response or runs a tool.
// New sheets need the same actual model preparation as generation; the caller
// owns that execution/admission/accounting. Frozen summaries need no new step.
// The returned plan counts the final model input, not preparation-model usage.
using ResponsesFilePreparation = std::function<ObjectReply(FileExpansion&)>;
ObjectReply PrepareResponsesTokenCount(
    const io::Json& request, const ChatPlanConfig& config, ObjectStore& objects,
    const StateAccess& access, ResponsesEnvelope* envelope,
    ResponsesGenerationPlan* generation,
    const ResponsesFilePreparation& prepare = {},
    FileExpansionUsage* file_usage = nullptr);

// Enforces the request deadline even when no storage read is necessary.
ObjectReply CheckResponsesTokenCountActive(const StateAccess& access);
io::Json ResponsesTokenCountJson(size_t tokens);

}  // namespace q4t::server
