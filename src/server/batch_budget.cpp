#include "q4t/server/batch_budget.h"

#include <algorithm>
#include <ctime>

#include "object_protocol.h"
#include "q4t/server/chat_file_inputs.h"
#include "q4t/server/input_contract.h"
#include "q4t/server/moderations_plan.h"
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

struct NativeMeter {
  const BinaryAccess& access;
  const BatchTokenCodec& codec;
  BatchInputBudget input;
  uint64_t output = 0;

  Status Encode(std::string_view text, std::vector<uint32_t>* tokens) {
    auto check = Active(access);
    if (!check.ok()) return Status::Fail(check.message);
    auto status = codec.encode(text, tokens);
    check = Active(access);
    return check.ok() ? status : Status::Fail(check.message);
  }
  Status Decode(const std::vector<uint32_t>& tokens, std::string* text) {
    auto check = Active(access);
    if (!check.ok()) return Status::Fail(check.message);
    auto status = codec.decode(tokens, text);
    check = Active(access);
    return check.ok() ? status : Status::Fail(check.message);
  }
  Status Chat(const Json& request, const ChatRequestPlan& plan,
              const ChatPlanConfig& config, ChatPlanError* error) {
    std::string prompt;
    std::vector<VisionItem> images;
    const auto& normalized = plan.Request(request);
    auto status = PrepareChatPrompt(normalized, plan.constraints, &prompt,
                                    &images, &error->param, &error->code);
    std::vector<uint32_t> tokens;
    ChatPromptBudget budget;
    if (status)
      status = EncodeChatPrompt(
          normalized, config, prompt,
          [&](auto text, auto* ids) { return Encode(text, ids); }, &tokens,
          &budget, error);
    if (status && !images.empty()) {
      if (!codec.expand_media) {
        *error = {"batch_media_count_unavailable", "input", 503};
        return Status::Fail("Batch visual input count is unavailable");
      }
      status = codec.expand_media(normalized, images, config, &tokens, &budget,
                                  error);
    }
    if (status) {
      input = {tokens.size(), true};
      output = budget.total_output;
    }
    return status;
  }
};

ObjectReply MeasureResponse(ResponsesExecution& response,
                            const ChatPlanConfig& config, NativeMeter* meter,
                            BatchRateBudget* rate) {
  if (!response.Prepared() || response.Finished()) return Invalid();
  auto planning = config;
  planning.prewarm_only = response.Envelope().cache.prewarm;
  if (response.PendingFileSheets()) {
    meter->input.tokens =
        config.client_limits
            ? std::min<uint64_t>(config.max_len,
                                 config.client_limits->max_prompt_tokens)
            : config.max_len;
    meter->input.exact = false;
  } else {
    if (!response.GenerationReady()) return Invalid();
    if (response.Envelope().truncate) {
      auto context = planning;
      context.client_limits = nullptr;
      auto reply = response.FitContext(
          planning,
          [&](const ResponsesGenerationPlan& candidate, ChatPlanError* error) {
            return meter->Chat(candidate.generation_request, candidate.chat,
                               context, error);
          });
      if (!reply.ok()) return reply;
    }
    const auto& generation = response.Generation();
    ChatPlanError error;
    const auto status = meter->Chat(generation.generation_request,
                                    generation.chat, planning, &error);
    if (!status) return Planning(status, error);
  }
  if (!rate) return {};
  BatchRateBudget budget;
  budget.prompt_capacity = meter->input.tokens;
  budget.input_exact = meter->input.exact;
  const auto& envelope = response.Envelope();
  const auto requested = envelope.options.GetInt("max_output_tokens");
  if (requested < 1) return Invalid();
  budget.output = envelope.cache.prewarm ? 0
                  : response.PendingFileSheets()
                      ? requested
                      : std::min<int64_t>(config.max_len, requested);
  budget.preparation_output = response.FileOutputBudget();
  budget.auxiliary_output =
      envelope.moderation.enabled
          ? (envelope.cache.prewarm ? 1 : 2) * kMaxModerationStepTokens
          : 0;
  if (response.VariableInputSteps()) {
    // Runtime enforces max_prompt_tokens across all actual steps. Reserve
    // that same ceiling so later generated prompts cannot race other jobs
    // for TPM after this request has already started. It is an upper bound.
    budget.input_exact = false;
    if (config.client_limits) {
      budget.prompt_capacity = config.client_limits->max_prompt_tokens;
    } else {
      // Anonymous operation has no minute quota. Still expose a finite bound:
      // each known sheet plus primary/pro/summary/compaction/two reviews.
      const auto* files = response.FileUsage();
      const uint64_t sheets = files ? files->sheets : 0;
      if (sheets > kBatchTokenLimit / config.max_len - 6) return Invalid();
      budget.prompt_capacity = (sheets + 6) * config.max_len;
    }
  }
  if (!budget.prompt_capacity || budget.Total() > kBatchTokenLimit ||
      budget.preparation_output > kMaxPreparationOutputTokens ||
      budget.auxiliary_output > kMaxAuxiliaryOutputTokens)
    return Invalid();
  if (config.client_limits &&
      budget.output > config.client_limits->max_output_tokens)
    return Error(400, "client_output_limit_exceeded",
                 "Batch public output exceeds current client limit");
  *rate = budget;
  return {};
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
                              BatchInputBudget* output, BatchRateBudget* rate) {
  auto active = Active(access);
  if (!active.ok()) return active;
  if (!output || !codec.encode || !codec.decode || config.max_len < 1 ||
      config.count_input_only || !prepared.IsObject())
    return Invalid();
  NativeMeter meter{access, codec, {}, 0};
  const auto encode = [&](auto text, auto* ids) {
    return meter.Encode(text, ids);
  };
  const auto decode = [&](const auto& ids, auto* text) {
    return meter.Decode(ids, text);
  };
  ChatPlanError error;
  BatchRateBudget planned;
  Status status;
  if (endpoint == "/v1/completions") {
    if (prepared.GetString("kind") != "completion") return Invalid();
    CompletionRequestPlan plan;
    status = PlanCompletionRequest(row.body, config, &plan, &error);
    if (status) {
      std::vector<PreparedCompletionPrompt> prompts;
      status =
          PrepareCompletionPrompts(plan, config, encode, decode, &prompts,
                                   &meter.input.tokens, &meter.output, &error);
    }
  } else if (endpoint == "/v1/chat/completions") {
    const auto* bindings = prepared.GetArray("input_snapshots");
    if (prepared.GetString("kind") != "chat" || !bindings) return Invalid();
    ChatRequestPlan plan;
    ChatFileInputs files(&objects);
    auto reply =
        files.Prepare(row.body, config, access, &plan, *bindings, true);
    if (!reply.ok()) return reply;
    status = meter.Chat(row.body, plan, config, &error);
  } else if (endpoint == "/v1/responses") {
    ResponsesExecution response(objects);
    auto reply = response.RestoreInputs(
        prepared, config, {access.scope, access.deadline, access.cancelled},
        std::time(nullptr));
    if (!reply.ok()) return reply;
    reply =
        MeasureResponse(response, config, &meter, rate ? &planned : nullptr);
    if (!reply.ok()) {
      active = Active(access);
      return active.ok() ? reply : active;
    }
  } else if (endpoint == "/v1/moderations") {
    if (prepared.GetString("kind") != "moderation") return Invalid();
    ModerationsRequestPlan plan;
    status = PlanModerationsRequest(row.body, config, &plan, &error);
    uint64_t total = 0;
    auto classifier_config = config;
    classifier_config.client_limits = nullptr;
    if (status) {
      for (const auto& input : plan.inputs) {
        status =
            meter.Chat(input.request, input.chat, classifier_config, &error);
        if (!status) break;
        if (meter.input.tokens > kBatchTokenLimit - total) return Invalid();
        total += meter.input.tokens;
      }
    }
    if (status && config.client_limits &&
        total > config.client_limits->max_prompt_tokens)
      return Error(400, "client_prompt_limit_exceeded",
                   "Moderation exceeds cumulative client input limit");
    meter.input = {total, true};
    meter.output = 0;
    planned.preparation_output = plan.OutputBudget();
  } else {
    return Error(503, "batch_endpoint_executor_unavailable",
                 "Batch endpoint requires its native token meter");
  }
  active = Active(access);
  if (!active.ok()) return active;
  if (!status) return Planning(status, error);
  if (!meter.input.tokens || meter.input.tokens > kBatchTokenLimit)
    return Invalid();
  if (rate && endpoint != "/v1/responses") {
    planned.prompt_capacity = meter.input.tokens;
    planned.output = meter.output;
    if (planned.Total() > kBatchTokenLimit) return Invalid();
  }
  *output = meter.input;
  if (rate) *rate = planned;
  return {};
}

ObjectReply MeasureBatchResponseRate(ResponsesExecution& response,
                                     const ChatPlanConfig& config,
                                     const BinaryAccess& access,
                                     const BatchTokenCodec& codec,
                                     BatchRateBudget* output) {
  auto active = Active(access);
  if (!active.ok()) return active;
  if (!output || !codec.encode || !codec.decode || config.max_len < 1 ||
      config.count_input_only || response.Scope() != access.scope)
    return Invalid();
  NativeMeter meter{access, codec, {}, 0};
  BatchRateBudget budget;
  auto reply = MeasureResponse(response, config, &meter, &budget);
  active = Active(access);
  if (!active.ok()) return active;
  if (!reply.ok()) return reply;
  *output = budget;
  return {};
}
}  // namespace q4t::server
