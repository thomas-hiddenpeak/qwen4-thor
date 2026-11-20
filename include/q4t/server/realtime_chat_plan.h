// Realtime's native text/tool request and actual context accounting.
#pragma once

#include "q4t/server/responses_plan.h"
#include "q4t/server/prompt_registry.h"
#include "q4t/server/realtime_speech_execution.h"
#include "q4t/server/realtime_mcp_context.h"

namespace q4t::server {
struct RealtimeChatPlan {
  ResponsesEnvelope envelope;
  ResponsesGenerationPlan generation;
  io::Json truncation;
  size_t prompt_items = 0;
  bool input_ready = false;
  RealtimeSpeechOptions speech;
};
// Pure preparation. References have already been resolved by the session owner.
// No model, network, file access, token reservation or response publication.
Status PlanRealtimeChat(const io::Json& settings, const io::Json& response,
                        const io::Json& items, const ChatPlanConfig& config,
                        RealtimeChatPlan* plan, ChatPlanError* error,
                        const PromptSnapshot* prompt = nullptr,
                        bool native_input = false,
                        const RealtimeMcpContext* mcp = nullptr,
                        bool mcp_definitions_only = false,
                        const io::Json* native_mcp_items = nullptr);
// Complete an envelope using owned, resolved content. File preparation must
// preserve the outer item ordering and count, so history indices remain valid.
// The media proof is internal and never parsed from a client's JSON.
Status CompleteRealtimeChatInput(const ChatPlanConfig& config,
                                 const io::Json& resolved,
                                 bool prepared_file_media,
                                 RealtimeChatPlan* plan, ChatPlanError* error);
// Project combined prefix/history indices back onto the public history.
// Hidden template items never address or delete conversation items.
std::vector<size_t> RealtimeRetainedHistory(const RealtimeChatPlan& plan);
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
