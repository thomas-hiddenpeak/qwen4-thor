#pragma once

#include <functional>

#include "q4t/server/model_preparation.h"
#include "q4t/server/moderations_plan.h"

namespace q4t::server {
struct ModerationStepResult {
  std::string content, finish_reason;
};
using ModerationStepExecutor = std::function<ObjectReply(
    const ModerationPlan&, ModelPreparation&, ModerationStepResult*)>;

// One existing request/rate/cancellation owner spans every classification.
// The executor runs actual native work; errors never become a safe report.
ObjectReply ExecuteModerationsRequest(std::string_view id,
                                      const ModerationsRequestPlan& plan,
                                      ClientAdmission& clients,
                                      const ClientIdentity& client,
                                      ClientRateLease& rate,
                                      const ModerationStepExecutor& execute,
                                      const std::function<bool()>& cancelled);
}  // namespace q4t::server
