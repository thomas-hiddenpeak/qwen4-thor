#include "q4t/server/batch_budget.h"

#include <algorithm>
#include <ctime>

#include "object_protocol.h"
#include "q4t/server/chat_file_inputs.h"
#include "q4t/server/input_contract.h"
#include "q4t/server/responses_execution.h"

namespace q4t::server {
namespace {
using namespace object_detail;
ObjectReply Active(const BinaryAccess& access) {
  if (access.scope.empty())
    return Error(500, "batch_budget_invalid", "Missing Batch client scope");
  if (std::chrono::steady_clock::now() >= access.deadline)
    return Error(408, "request_deadline_exceeded", "Batch token count expired");
  if (access.cancelled && access.cancelled())
    return Error(499, "request_cancelled", "Batch token count cancelled");
  return {};
}
ObjectReply Invalid() {
  return Error(503, "batch_budget_invalid", "Invalid Batch token measurement");
}
ObjectReply Planning(const Status& status, const ChatPlanError& error) {
  auto reply = Error(error.http_status, error.code, status.message());
  reply.value = Object({{"param", String(error.param)}});
  return reply;
}
}  // namespace

Json BatchInputBudgetJson(const BatchInputBudget& budget) {
  return Object(
      {{"tokens", Number(budget.tokens)},
       {"mode", String(budget.exact ? "exact" : "context_upper_bound")}});
}
bool ParseBatchInputBudget(const Json& value, BatchInputBudget* budget) {
  const auto* tokens = value.Find("tokens");
  const auto mode = value.GetString("mode");
  if (!budget || !value.IsObject() || value.object.size() != 2 || !tokens ||
      !IsInputInteger(*tokens) || tokens->AsInt() <= 0 ||
      static_cast<uint64_t>(tokens->AsInt()) > kBatchTokenLimit ||
      (mode != "exact" && mode != "context_upper_bound"))
    return false;
  *budget = {static_cast<uint64_t>(tokens->AsInt()), mode == "exact"};
  return true;
}

ObjectReply MeasureBatchInput(ObjectStore& objects, std::string_view endpoint,
                              const BatchInputRow& row, const Json& prepared,
                              const ChatPlanConfig& config,
                              const BinaryAccess& access,
                              const BatchTokenCodec& codec,
                              BatchInputBudget* output) {
  auto active = Active(access);
  if (!active.ok()) return active;
  if (!output || !codec.encode || !codec.decode || config.max_len < 1 ||
      config.count_input_only || !prepared.IsObject())
    return Invalid();
  // Cancellation still wins over a codec's generic parse/encode error.
  const auto encode = [&](std::string_view text,
                          std::vector<uint32_t>* tokens) -> Status {
    auto check = Active(access);
    if (!check.ok()) return Status::Fail(check.message);
    auto status = codec.encode(text, tokens);
    check = Active(access);
    return check.ok() ? status : Status::Fail(check.message);
  };
  const auto decode = [&](const std::vector<uint32_t>& tokens,
                          std::string* text) -> Status {
    auto check = Active(access);
    if (!check.ok()) return Status::Fail(check.message);
    auto status = codec.decode(tokens, text);
    check = Active(access);
    return check.ok() ? status : Status::Fail(check.message);
  };
  ChatPlanError error;
  BatchInputBudget measured;
  const auto chat_tokens = [&](const Json& request, const ChatRequestPlan& plan,
                               const ChatPlanConfig& options,
                               ChatPlanError* failure) -> Status {
    std::string prompt;
    std::vector<VisionItem> images;
    const auto& normalized = plan.Request(request);
    auto status = PrepareChatPrompt(normalized, plan.constraints, &prompt,
                                    &images, &failure->param, &failure->code);
    std::vector<uint32_t> tokens;
    ChatPromptBudget budget;
    if (status)
      status = EncodeChatPrompt(normalized, options, prompt, encode, &tokens,
                                &budget, failure);
    if (status && !images.empty()) {
      if (!codec.expand_media) {
        *failure = {"batch_media_count_unavailable", "input", 503};
        return Status::Fail("Batch visual input count is unavailable");
      }
      status = codec.expand_media(normalized, images, options, &tokens, &budget,
                                  failure);
    }
    if (status) measured = {tokens.size(), true};
    return status;
  };
  Status status;
  if (endpoint == "/v1/completions") {
    if (prepared.GetString("kind") != "completion") return Invalid();
    CompletionRequestPlan plan;
    status = PlanCompletionRequest(row.body, config, &plan, &error);
    if (status) {
      std::vector<PreparedCompletionPrompt> prompts;
      uint64_t outputs = 0;
      status = PrepareCompletionPrompts(plan, config, encode, decode, &prompts,
                                        &measured.tokens, &outputs, &error);
    }
  } else if (endpoint == "/v1/chat/completions") {
    const auto* bindings = prepared.GetArray("input_snapshots");
    if (prepared.GetString("kind") != "chat" || !bindings) return Invalid();
    ChatRequestPlan plan;
    ChatFileInputs files(&objects);
    auto reply =
        files.Prepare(row.body, config, access, &plan, *bindings, true);
    if (!reply.ok()) return reply;
    status = chat_tokens(row.body, plan, config, &error);
  } else if (endpoint == "/v1/responses") {
    ResponsesExecution response(objects);
    auto reply = response.RestoreInputs(
        prepared, config, {access.scope, access.deadline, access.cancelled},
        std::time(nullptr));
    if (!reply.ok()) return reply;
    if (response.PendingFileSheets()) {
      // A final prompt containing a model-generated summary is not known yet.
      // Actual preparation and generation are still fully charged to TPM.
      measured.tokens =
          config.client_limits
              ? std::min<uint64_t>(config.max_len,
                                   config.client_limits->max_prompt_tokens)
              : config.max_len;
      measured.exact = false;
    } else {
      if (!response.GenerationReady()) return Invalid();
      if (response.Envelope().truncate) {
        auto context = config;
        context.client_limits = nullptr;
        reply = response.FitContext(
            config, [&](const ResponsesGenerationPlan& candidate,
                        ChatPlanError* failure) {
              return chat_tokens(candidate.generation_request, candidate.chat,
                                 context, failure);
            });
        if (!reply.ok()) {
          active = Active(access);
          return active.ok() ? reply : active;
        }
      }
      const auto& generation = response.Generation();
      status = chat_tokens(generation.generation_request, generation.chat,
                           config, &error);
    }
  } else {
    return Error(503, "batch_endpoint_executor_unavailable",
                 "Batch endpoint requires its native token meter");
  }
  active = Active(access);
  if (!active.ok()) return active;
  if (!status) return Planning(status, error);
  if (!measured.tokens || measured.tokens > kBatchTokenLimit) return Invalid();
  *output = measured;
  return {};
}
}  // namespace q4t::server
