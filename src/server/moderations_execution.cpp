#include "q4t/server/moderations_execution.h"

#include "object_protocol.h"
#include "q4t/server/lifetime.h"

namespace q4t::server {
ObjectReply ExecuteModerationsRequest(std::string_view id,
                                      const ModerationsRequestPlan& plan,
                                      ClientAdmission& clients,
                                      const ClientIdentity& client,
                                      ClientRateLease& rate,
                                      const ModerationStepExecutor& execute,
                                      const std::function<bool()>& cancelled) {
  using object_detail::Error;
  if (plan.inputs.empty() || plan.inputs.size() > kModerationsInputs ||
      plan.model.empty() || !client.Allowed() || !execute || !cancelled)
    return Error(500, "moderation_state_invalid",
                 "Invalid moderation execution ownership");
  ModelPreparation accounting(clients, client, rate, 0, 0, plan.OutputBudget());
  std::vector<io::Json> reports;
  reports.reserve(plan.inputs.size());
  for (const auto& input : plan.inputs) {
    if (cancelled())
      return Error(409, "moderation_cancelled",
                   "Moderation request was cancelled");
    ModerationStepResult step;
    ScopeExit close([&] { accounting.CloseStep(); });
    auto reply = execute(input, accounting, &step);
    if (!reply.ok()) return reply;
    if (cancelled())
      return Error(409, "moderation_cancelled",
                   "Moderation request was cancelled");
    io::Json report;
    const auto status =
        step.finish_reason == "stop"
            ? ParseModerationResult(step.content, input, 0.5, &report)
            : Status::Fail("Classification did not complete its score set");
    if (!status)
      return Error(502, "moderation_scores_invalid", status.message());
    reports.push_back(std::move(report));
  }
  ObjectReply reply;
  const auto status = BuildModerationsResponse(id, plan, reports, &reply.value);
  return status ? reply
                : Error(502, "moderation_scores_invalid", status.message());
}
}  // namespace q4t::server
