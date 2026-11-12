#pragma once

#include <string_view>

#include "q4t/server/responses_plan.h"

namespace q4t::server {

// Native pro execution: one bounded deliberation, then the original answer
// contract with the actual notes. Both steps share the external token budget.
// The planner never executes tools or alters persistent input history.
Status PlanResponsesDeliberation(const ResponsesGenerationPlan& source,
                                 int output_limit, int64_t output_used,
                                 const ChatPlanConfig& config,
                                 ResponsesSummaryPlan* plan,
                                 ChatPlanError* error);
Status PlanResponsesDeliberatedAnswer(const ResponsesGenerationPlan& source,
                                      std::string_view notes, int output_limit,
                                      int64_t output_used,
                                      const ChatPlanConfig& config,
                                      ResponsesGenerationPlan* plan,
                                      ChatPlanError* error);

}  // namespace q4t::server
