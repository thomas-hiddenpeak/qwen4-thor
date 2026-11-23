#pragma once

#include <span>

#include "q4t/server/actual_usage.h"
#include "q4t/server/chat_plan.h"

namespace q4t::server {
enum class DecisionShape { kRequest, kResult };
Status ValidateDecisionShape(const io::Json& value, DecisionShape shape,
                             ChatPlanError* error);
// Additional resource and field limits after the caller's SDK shape check.
Status CheckDecisionInputItem(const io::Json& item, const std::string& path,
                              size_t* breakpoints, ChatPlanError* error);
Status CheckDecisionRequest(const io::Json& request,
                            const ChatPlanConfig& config, ChatPlanError* error);
// An execution supplies one log weight per option in the requested order.
// Predicate options are false, true. Refusal is a distinct model outcome.
Status ProjectDecisionAnswer(const io::Json& question,
                             std::span<const double> log_weights,
                             double confidence, bool refused, io::Json* answer);
Status BuildDecisionResult(const io::Json& request, io::Json answers,
                           const ActualUsage& usage, io::Json* result);
}  // namespace q4t::server
