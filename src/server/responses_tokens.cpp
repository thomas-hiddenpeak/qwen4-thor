#include "q4t/server/responses_tokens.h"

#include <algorithm>
#include <ctime>

#include "object_protocol.h"
#include "q4t/server/input_contract.h"
#include "q4t/server/lifetime.h"
#include "q4t/server/responses_execution.h"

namespace q4t::server {
namespace {
using namespace object_detail;

ObjectReply Invalid(const Status& status, const ChatPlanError& error) {
  auto result = Error(error.http_status, error.code, status.message());
  result.value = Object({{"param", String(error.param)}});
  return result;
}
}  // namespace

ObjectReply CheckResponsesTokenCountActive(const StateAccess& access) {
  if (std::chrono::steady_clock::now() >= access.deadline)
    return Error(408, "request_deadline_exceeded", "Input counting timed out");
  if (access.cancelled && access.cancelled())
    return Error(499, "request_cancelled", "Input counting cancelled");
  return {};
}

ObjectReply PrepareResponsesTokenCount(
    const io::Json& request, const ChatPlanConfig& config, ObjectStore& objects,
    const StateAccess& access, ResponsesEnvelope* envelope,
    ResponsesGenerationPlan* generation,
    const ResponsesFilePreparation& prepare, FileExpansionUsage* file_usage,
    IResponsesCleanup* cleanup, bool require_cleanup,
    uint64_t* preparation_output_budget) {
  *envelope = {};
  *generation = {};
  if (file_usage) *file_usage = {};
  if (preparation_output_budget) *preparation_output_budget = 0;
  auto active = CheckResponsesTokenCountActive(access);
  if (!active.ok()) return active;
  if (!config.count_input_only)
    return Error(500, "invalid_count_configuration",
                 "Input count requires count-only planning");
  auto normalized = request.IsNull() ? Object() : request;
  ChatPlanError error;
  auto status =
      InputFields(normalized,
                  {"conversation", "input", "instructions", "model",
                   "parallel_tool_calls", "personality", "previous_response_id",
                   "reasoning", "text", "tool_choice", "tools", "truncation"},
                  &error.param, "");
  if (!status) return Invalid(status, error);
  // These two SDK fields are non-nullable; generation's broader nullable
  // options must not erase the count endpoint's own contract.
  for (const auto* key : {"truncation", "personality"})
    if (const auto* value = normalized.Find(key); value && value->IsNull())
      return Invalid(Status::Fail("expected string"),
                     {"invalid_request", key, 400});
  // Unlike Responses create, the count SDK permits null for these fields.
  // Resolve only count's nullable defaults; never relax create's contract.
  std::erase_if(normalized.object, [](const auto& field) {
    const auto& key = field.first;
    return field.second.IsNull() &&
           (key == "input" || key == "model" || key == "text" ||
            key == "tools" || key == "tool_choice");
  });
  Set(&normalized, "store", Boolean(false));
  ResponsesExecution execution(objects, cleanup, require_cleanup);
  ScopeExit close([&] {
    try {
      execution.CloseUnfinished();
    } catch (...) {
      // Its cleanup owner retains tracked resources across storage failures.
    }
  });
  auto result =
      execution.Prepare(normalized, config, access, std::time(nullptr));
  if (!result.ok() || result.commit_unknown) return result;
  if (preparation_output_budget)
    *preparation_output_budget = execution.FileOutputBudget();
  result = execution.PrepareInputSheets(config, prepare);
  if (!result.ok() || result.commit_unknown) return result;
  if (file_usage) *file_usage = *execution.FileUsage();
  *envelope = execution.Envelope();
  *generation = execution.Generation();
  return CheckResponsesTokenCountActive(access);
}

io::Json ResponsesTokenCountJson(size_t tokens) {
  return Object({{"object", String("response.input_tokens")},
                 {"input_tokens", Number(static_cast<int64_t>(tokens))}});
}

}  // namespace q4t::server
