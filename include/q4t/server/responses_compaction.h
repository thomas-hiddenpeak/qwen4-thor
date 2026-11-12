#pragma once

#include "q4t/server/responses_plan.h"
#include "q4t/server/state_cipher.h"

namespace q4t::server {
enum class ResponsesCompactionMode { kAutomatic, kStandalone };

// Immutable source and retained items. Only actual model output can become
// memory; the caller must measure the resulting template/token count before
// publishing or using a compacted window.
struct ResponsesCompactionPlan {
  io::Json source;
  io::Json retained;
  size_t memory_index = 0;
  size_t compacted_items = 0;
  io::Json request;
  ChatRequestPlan chat;
};

Status PlanResponsesCompaction(const io::Json& history,
                               ResponsesCompactionMode mode, int output_limit,
                               const ChatPlanConfig& config,
                               ResponsesCompactionPlan* plan,
                               ChatPlanError* error);

// The wrapper carries its original item ID because the public input contract
// permits an omitted ID. The ID is authenticated by the inner AEAD binding.
std::string ResponsesCompactionId(std::string_view opaque);
// Return replayable public input forms. Canonical stored non-assistant
// messages have server IDs, but the SDK input message contract omits them.
Status ResponsesCompactionWindow(const io::Json& retained,
                                 const io::Json& compaction, io::Json* window,
                                 ChatPlanError* error);
ObjectReply SealResponsesCompaction(StateCipher& cipher,
                                    const ResponsesCompactionPlan& plan,
                                    std::string_view actual_memory,
                                    const std::string& item_id,
                                    const ChatPlanConfig& config,
                                    const StateAccess& access, io::Json* item,
                                    io::Json* expanded);
// supplied_prefix may be the complete original history, the returned retained
// window, or empty (stateless chaining after the latest compaction item).
// Conflicting plaintext never replaces authenticated state.
ObjectReply OpenResponsesCompaction(StateCipher& cipher,
                                    const io::Json& supplied_prefix,
                                    const io::Json& item,
                                    const ChatPlanConfig& config,
                                    const StateAccess& access,
                                    io::Json* expanded);
}  // namespace q4t::server
