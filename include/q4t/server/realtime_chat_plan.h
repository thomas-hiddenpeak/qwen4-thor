// Realtime's native text/tool request and actual context accounting.
#pragma once

#include "q4t/server/responses_plan.h"

namespace q4t::server {
struct RealtimeChatPlan {
  ResponsesEnvelope envelope;
  ResponsesGenerationPlan generation;
  io::Json truncation;
};
// Pure preparation. References have already been resolved by the session owner.
// No model, network, file access, token reservation or response publication.
Status PlanRealtimeChat(const io::Json& settings, const io::Json& response,
                        const io::Json& items, const ChatPlanConfig& config,
                        RealtimeChatPlan* plan, ChatPlanError* error);
// The callback counts the actual native template/tokenizer (and media, if
// present). Client quotas remain enforced by the eventual ordinary Chat owner.
using RealtimeContextMeasure = std::function<Status(
    const ResponsesGenerationPlan&, size_t*, ChatPlanError*)>;
Status FitRealtimeChatContext(const ChatPlanConfig& config,
                              const RealtimeContextMeasure& measure,
                              RealtimeChatPlan* plan, ChatPlanError* error);
io::Json DefaultRealtimeChatSettings(const ChatPlanConfig& config,
                                     std::string model);
Status ParseRealtimeChatTarget(std::string_view target,
                               const ChatPlanConfig& config, io::Json* settings,
                               ChatPlanError* error);
}  // namespace q4t::server
