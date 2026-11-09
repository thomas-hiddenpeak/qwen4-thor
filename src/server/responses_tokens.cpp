#include "q4t/server/responses_tokens.h"

#include <algorithm>

#include "object_protocol.h"
#include "q4t/server/input_contract.h"
#include "q4t/server/responses_crypto.h"

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

ObjectReply PrepareResponsesTokenCount(const io::Json& request,
                                       const ChatPlanConfig& config,
                                       ObjectStore& objects,
                                       const StateAccess& access,
                                       ResponsesEnvelope* envelope,
                                       ResponsesGenerationPlan* generation) {
  *envelope = {};
  *generation = {};
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
  status = ParseResponsesEnvelope(normalized, config, envelope, &error);
  if (!status) return Invalid(status, error);
  ResponsesState state(objects);
  ResponseStatePlan resolved;
  auto result = state.Resolve(*envelope, access, &resolved);
  if (!result.ok()) return result;
  // A retained compaction trigger is bookkeeping, not a model message. A
  // count request never executes compaction, even for a new trigger.
  std::erase_if(resolved.history.array, [](const auto& item) {
    return item.GetString("type") == "compaction_trigger";
  });
  StateCipher cipher(objects);
  io::Json history;
  const bool encrypted =
      std::any_of(resolved.history.array.begin(), resolved.history.array.end(),
                  [](const auto& item) {
                    const auto* value = item.Find("encrypted_content");
                    return (item.GetString("type") == "reasoning" ||
                            item.GetString("type") == "compaction") &&
                           value && !value->IsNull();
                  });
  if (encrypted) {
    result = OpenResponsesReasoning(cipher, resolved.history, config, access,
                                    &history);
    if (!result.ok()) return result;
  } else {
    history = std::move(resolved.history);
  }
  status = PrepareResponsesGeneration(*envelope, history, config, generation,
                                      &error);
  if (!status) return Invalid(status, error);
  return CheckResponsesTokenCountActive(access);
}

io::Json ResponsesTokenCountJson(size_t tokens) {
  return Object({{"object", String("response.input_tokens")},
                 {"input_tokens", Number(static_cast<int64_t>(tokens))}});
}

}  // namespace q4t::server
