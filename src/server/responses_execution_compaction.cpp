#include "q4t/server/responses_execution.h"

#include <algorithm>
#include <stdexcept>

#include "object_protocol.h"
#include "q4t/server/http_response.h"

namespace q4t::server {
namespace {
using namespace object_detail;
ObjectReply Failure(const Status& status, const ChatPlanError& error) {
  auto reply = Error(error.http_status, error.code, status.message());
  reply.value = Object({{"param", String(error.param)}});
  return reply;
}
}  // namespace

ObjectReply ResponsesExecution::SelectCompaction(size_t input_tokens,
                                                 const ChatPlanConfig& config) {
  if (!prepared_ || !generation_ready_ || plan_.finished ||
      compaction_selected_ ||
      !output_.Snapshot().GetArray("output")->array.empty())
    return Error(500, "response_state_invalid", "Invalid compaction selection");
  bool selected = standalone_compaction_ || compaction_trigger_;
  if (const auto* policies = envelope_.request.GetArray("context_management");
      policies && !policies->array.empty()) {
    // Default early enough to leave space for the continuation. This is a
    // rendered token threshold, never an estimate from character count.
    const auto threshold = policies->array[0].GetInt(
        "compact_threshold",
        std::max<int64_t>(1, int64_t(config.max_len) * 4 / 5));
    selected |= input_tokens >= static_cast<size_t>(threshold);
  }
  if (!selected) return {};
  const int budget = envelope_.options.GetInt("max_output_tokens");
  const int memory_budget =
      standalone_compaction_ ? budget : std::min(512, std::max(1, budget / 2));
  ResponsesCompactionPlan candidate;
  ChatPlanError error;
  auto status = PlanResponsesCompaction(
      generation_history_,
      standalone_compaction_ ? ResponsesCompactionMode::kStandalone
                             : ResponsesCompactionMode::kAutomatic,
      memory_budget, config, &candidate, &error);
  if (!status) return Failure(status, error);
  if (candidate.retained.array.size() >= 4096)
    return Error(400, "compaction_output_limit",
                 "Retained items leave no room for a compaction item");
  if (candidate.compacted_items && !standalone_compaction_ && budget < 2)
    return Error(400, "compaction_budget_exhausted",
                 "Compaction and continuation require at least two tokens");
  if (candidate.compacted_items &&
      !envelope_.access_program.instructions.empty()) {
    AddAccessProgramInstructions(envelope_.access_program,
                                 &Field(candidate.request, "messages"));
    status =
        PlanChatRequest(candidate.request, config, &candidate.chat, &error);
    if (!status) return Failure(status, error);
  }
  if (standalone_compaction_ && candidate.compacted_items) {
    const auto instructions = envelope_.options.GetString("instructions");
    if (!instructions.empty()) {
      auto& messages = Field(candidate.request, "messages").array;
      messages.insert(messages.end() - 1,
                      Object({{"role", String("developer")},
                              {"content", String(instructions)}}));
      status =
          PlanChatRequest(candidate.request, config, &candidate.chat, &error);
      if (!status) return Failure(status, error);
    }
  }
  auto ready = cipher_.Ready(access_);
  if (!ready.ok()) return ready;
  compaction_ = std::move(candidate);
  compaction_source_tokens_ = input_tokens;
  compaction_id_ = detail::NewRequestId("cmp_");
  compaction_selected_ = true;
  return {};
}

ObjectReply ResponsesExecution::StartCompaction(std::string* wire) {
  wire->clear();
  if (!begun_ || plan_.finished || commit_unknown_ || !compaction_selected_ ||
      compaction_started_)
    return Error(500, "response_state_invalid", "Invalid compaction start");
  auto status =
      RecordOutput(output_.StartCompaction(compaction_id_, wire), wire);
  if (!status)
    return Error(503, "response_storage_exhausted", status.message());
  compaction_started_ = true;
  return {};
}

ObjectReply ResponsesExecution::CompleteCompaction(
    std::string_view memory, int64_t output_used, const ChatPlanConfig& config,
    const ContextMeasure& measure, std::string* wire) {
  wire->clear();
  if (!begun_ || plan_.finished || commit_unknown_ || !compaction_started_ ||
      compaction_completed_ || !measure || output_used < 0 ||
      output_used > envelope_.options.GetInt("max_output_tokens"))
    return Error(500, "response_state_invalid",
                 "Invalid compaction completion");
  Json item, expanded;
  auto reply =
      SealResponsesCompaction(cipher_, compaction_, memory, compaction_id_,
                              config, access_, &item, &expanded);
  if (!reply.ok()) return reply;
  auto continuation = envelope_;
  // Independent compact ends here; a regular response must reserve at least
  // one token for its primary answer, never refill the original output limit.
  const auto remaining =
      envelope_.options.GetInt("max_output_tokens") - output_used;
  if (!standalone_compaction_ && remaining <= 0)
    return Error(502, "compaction_budget_exhausted",
                 "No output budget remains for continuation");
  Set(&continuation.options, "max_output_tokens",
      Number(standalone_compaction_ ? 1 : remaining));
  ResponsesGenerationPlan generation;
  ChatPlanError error;
  auto status = PrepareResponsesGeneration(continuation, expanded, config,
                                           &generation, &error);
  size_t tokens = 0;
  if (status) status = measure(generation, &tokens, &error);
  if (!status) return Failure(status, error);
  if (compaction_.compacted_items && tokens >= compaction_source_tokens_)
    return Error(502, "compaction_not_reduced",
                 "Generated memory did not reduce the rendered context");
  if (std::chrono::steady_clock::now() >= access_.deadline)
    return Error(408, "request_deadline_exceeded", "Compaction expired");
  if (access_.cancelled && access_.cancelled())
    return Error(499, "request_cancelled", "Compaction cancelled");
  Json window;
  status =
      ResponsesCompactionWindow(compaction_.retained, item, &window, &error);
  if (!status) return Failure(status, error);
  status = RecordOutput(output_.CompleteCompaction(item, wire), wire);
  if (!status)
    return Error(503, "response_storage_exhausted", status.message());
  generation_ = std::move(generation);
  generation_history_ = std::move(expanded);
  compaction_window_ = std::move(window);
  compaction_completed_ = true;
  return {};
}

std::string ResponsesExecution::ResultJson() const {
  if (!standalone_compaction_)
    return HasFileSearch() ? OutputJson(PublicResponse()) : output_.Json();
  const auto& snapshot = output_.Snapshot();
  // Only publish this projection after the same terminal/usage checks as
  // ordinary generation. The original full input is never returned as memory.
  if (!plan_.finished || !compaction_completed_ ||
      snapshot.GetString("status") != "completed")
    throw std::logic_error("Compact result is not committed");
  return OutputJson(
      ResponsesPublicValue(Object({{"id", *snapshot.Find("id")},
                                   {"object", String("response.compaction")},
                                   {"created_at", *snapshot.Find("created_at")},
                                   {"output", compaction_window_},
                                   {"usage", *snapshot.Find("usage")}})));
}
}  // namespace q4t::server
