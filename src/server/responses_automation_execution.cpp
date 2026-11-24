#include "q4t/server/responses_execution.h"

#include <algorithm>
#include <ctime>

#include "agent_automation_tool.h"
#include "object_protocol.h"
#include "q4t/server/http_response.h"
#include "q4t/server/request_json.h"

namespace q4t::server {
using namespace object_detail;

ObjectReply ResponsesExecution::RunSessionAutomations(
    size_t completed_calls, const ChatPlanConfig& config,
    bool dispatcher_available, const std::function<void()>& wake,
    const std::function<bool(const std::string&)>& publish) {
  if (!begun_ || plan_.finished || commit_unknown_ || !session_automations_ ||
      !PendingAutomations() || plan_.agent_session_id.empty() ||
      agent_session_id_ != plan_.agent_session_id || moderation_safe_)
    return Error(409, "response_state_invalid",
                 "No eligible session automation dispatch");
  const auto limit =
      std::min<uint64_t>(kMaxNativeToolCalls,
                         envelope_.options.GetInt("max_tool_calls", INT64_MAX));
  // Validate the whole model step before publishing any of its side effects.
  std::vector<io::Json> arguments;
  for (const auto& [index, call] : automation_pending_) {
    auto& parsed = arguments.emplace_back();
    if (index >= completed_calls || !ValidId(call.id) ||
        !io::ParseJson(call.arguments, &parsed, true, kSchemaJsonLimits) ||
        !parsed.IsObject())
      return Error(502, "automation_arguments_invalid",
                   "Incomplete generated automation arguments");
  }
  size_t i = 0;
  for (const auto& [index, call] : automation_pending_) {
    if (access_.cancelled && access_.cancelled())
      return Error(499, "request_cancelled", "Session automation cancelled");
    if (std::chrono::steady_clock::now() >= access_.deadline)
      return Error(408, "request_deadline_exceeded",
                   "Session deadline expired");
    automation_detail::SessionAutomationChange change{
        agent_session_id_, call.id, {}};
    io::Json result;
    if (native_tool_calls_ >= limit) {
      result = Object({{"error", String("tool_call_budget_exhausted")}});
    } else {
      ++native_tool_calls_;
      auto planned = automation_detail::PlanSessionTool(
          objects_, access_, config, arguments[i], std::time(nullptr),
          dispatcher_available, &change);
      if (planned.commit_unknown || planned.http_status >= 500 ||
          planned.http_status == 408 || planned.http_status == 499)
        return BindMutationResult(std::move(planned));
      if (planned.ok()) {
        result = std::move(planned.value);
      } else {
        result = Object({{"error", String(planned.code)},
                         {"message", String(planned.ClientMessage())}});
        if (const auto* field = planned.value.Find("param"))
          Set(&result, "param", *field);
      }
    }
    ++i;
    auto item = Object({{"type", String("function_call")},
                        {"name", String(automation_detail::kToolName)},
                        {"id", String(detail::NewRequestId("agentauto_"))},
                        {"call_id", String(call.id)},
                        {"status", String("completed")},
                        {"arguments", String(call.arguments)}});
    auto receipt = Object({{"type", String("function_call_output")},
                           {"name", String(automation_detail::kToolName)},
                           {"id", String(detail::NewRequestId("agentauto_"))},
                           {"call_id", String(call.id)},
                           {"status", String("completed")},
                           {"output", String(OutputJson(result))}});
    auto candidate = output_;
    auto events = events_;
    size_t bytes = event_bytes_;
    std::string wire, tail;
    auto status = candidate.AppendAgentItem(item, &wire);
    if (status) status = candidate.AppendAgentItem(receipt, &tail);
    wire += tail;
    if (status) status = Capture(wire, &events, &bytes);
    if (!status)
      return Error(503, "response_storage_exhausted", status.message());
    // Publishing a success before this CAS could expose a wakeup that never
    // existed. Publishing the wakeup alone could repeat it after a crash.
    auto saved = BindMutationResult(
        state_.Checkpoint(candidate.Snapshot(), events, access_, &plan_,
                          change.write ? &*change.write : nullptr));
    if (!saved.ok()) return saved;
    output_ = std::move(candidate);
    events_ = std::move(events);
    event_bytes_ = checkpoint_bytes_ = bytes;
    checkpoint_time_ = std::chrono::steady_clock::now();
    if (change.write && wake) wake();
    if (publish && !publish(wire))
      return Error(499, "request_cancelled", "Session subscriber cancelled");
  }
  automation_pending_.clear();
  automation_argument_bytes_ = 0;
  return {};
}
}  // namespace q4t::server
