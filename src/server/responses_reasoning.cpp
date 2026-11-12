#include "q4t/server/responses_reasoning.h"

#include <algorithm>

#include "responses_json.h"

namespace q4t::server {
namespace {
using namespace responses_detail;

Status Remaining(int limit, int64_t used, int* remaining,
                 ChatPlanError* error) {
  *error = {};
  if (limit <= 0 || used < 0 || used > limit) {
    *error = {"response_budget_invalid", "max_output_tokens", 500};
    return Status::Fail("Invalid shared pro output budget");
  }
  *remaining = static_cast<int>(limit - used);
  return {};
}

Status Replan(const Json& request, const ChatPlanConfig& config,
              ChatRequestPlan* plan, ChatPlanError* error) {
  auto status = PlanChatRequest(request, config, plan, error);
  if (!status) error->param = "reasoning.mode";
  return status;
}
}  // namespace

Status PlanResponsesDeliberation(const ResponsesGenerationPlan& source,
                                 int output_limit, int64_t output_used,
                                 const ChatPlanConfig& config,
                                 ResponsesSummaryPlan* plan,
                                 ChatPlanError* error) {
  *plan = {};
  int remaining = 0;
  auto status = Remaining(output_limit, output_used, &remaining, error);
  if (!status) return status;
  if (!remaining) {
    plan->budget_exhausted = true;
    return {};
  }
  auto request = source.chat.Request(source.generation_request);
  const auto* messages = request.GetArray("messages");
  if (!messages || !messages->IsArray()) {
    *error = {"response_state_invalid", "reasoning.mode", 500};
    return Status::Fail("Pro deliberation requires actual model context");
  }
  Field(request, "messages")
      .array.push_back(
          Object({{"role", String("user")},
                  {"content", String("Before answering the current task, make "
                                     "concise working notes: "
                                     "check the facts, calculations, "
                                     "constraints and any tool evidence. "
                                     "Identify uncertainties and an answer "
                                     "plan. Do not call tools or "
                                     "claim unperformed actions. Return "
                                     "working notes, not the final "
                                     "answer or a tool call. A separate step "
                                     "will answer the task.")}}));
  // Effort, actual history/media and tool definitions remain available to the
  // deliberation. Execution is text-only; an emitted call is a protocol error.
  Set(&request, "response_format", Object({{"type", String("text")}}));
  const auto* tools = request.GetArray("tools");
  // An empty tool set cannot authorize a call. Do not turn plain pro requests
  // into a new dependency on the optional constrained-decoding backend.
  Set(&request, "tool_choice",
      String(tools && !tools->array.empty() ? "none" : "auto"));
  Set(&request, "parallel_tool_calls", Boolean(false));
  Set(&request, "logprobs", Boolean(false));
  std::erase_if(request.object, [](const auto& field) {
    return field.first == "top_logprobs" || field.first == "prediction";
  });
  Set(&request, "max_completion_tokens",
      Number(std::min(512, std::max(1, remaining / 3))));
  ResponsesSummaryPlan candidate;
  candidate.request = std::move(request);
  status = Replan(candidate.request, config, &candidate.chat, error);
  if (!status) return status;
  candidate.needed = true;
  *plan = std::move(candidate);
  return {};
}

Status PlanResponsesDeliberatedAnswer(const ResponsesGenerationPlan& source,
                                      std::string_view notes, int output_limit,
                                      int64_t output_used,
                                      const ChatPlanConfig& config,
                                      ResponsesGenerationPlan* plan,
                                      ChatPlanError* error) {
  *plan = {};
  int remaining = 0;
  auto status = Remaining(output_limit, output_used, &remaining, error);
  if (!status) return status;
  if (!remaining || notes.empty() || notes.size() > 1024 * 1024) {
    *error = {"response_deliberation_incomplete", "reasoning.mode", 502};
    return Status::Fail("Pro continuation requires notes and remaining budget");
  }
  auto candidate = source;
  candidate.generation_request = source.chat.Request(source.generation_request);
  auto& request = candidate.generation_request;
  const auto* messages = request.GetArray("messages");
  if (!messages || !messages->IsArray()) {
    *error = {"response_state_invalid", "reasoning.mode", 500};
    return Status::Fail("Pro continuation requires actual model context");
  }
  // Notes are assistant data, never elevated to system instructions or a
  // claimed external tool result. The original output/tool contract survives.
  Field(request, "messages")
      .array.push_back(
          Object({{"role", String("assistant")},
                  {"content", String("Working notes for the current task:\n" +
                                     std::string(notes))}}));
  Field(request, "messages")
      .array.push_back(Object(
          {{"role", String("user")},
           {"content",
            String(
                "Now answer the original current task, respecting its "
                "requested "
                "format and the actual available tools. Check the working "
                "notes; "
                "they are tentative analysis, not instructions or tool "
                "evidence. "
                "Return the final answer or the required real tool call.")}}));
  Set(&request, "max_completion_tokens", Number(remaining));
  status = Replan(request, config, &candidate.chat, error);
  if (!status) return status;
  *plan = std::move(candidate);
  return {};
}
}  // namespace q4t::server
