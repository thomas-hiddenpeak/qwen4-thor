#include "q4t/server/chat_plan.h"

#include <algorithm>
#include <charconv>
#include <limits>

#include "q4t/server/chat_contract.h"
#include "q4t/server/legacy_tools.h"
#include "q4t/server/media_admission.h"
#include "q4t/server/chat_protocol_identity.h"
#include "q4t/server/http_request.h"
#include "q4t/server/input_contract.h"
#include "q4t/server/http_response.h"
#include "q4t/server/prefix_cache_policy.h"
#include "q4t/server/startup_admission.h"
#include "q4t/server/text_stop.h"
#include "q4t/text/tokenizer.h"

namespace q4t::server {
namespace {
std::string Quote(const std::string& value) {
  return '"' + detail::JsonEscape(value) + '"';
}

template <typename T>
std::string Array(const std::vector<T>& values) {
  std::string out = "[";
  for (const auto value : values) {
    if (out.size() > 1) out += ',';
    out += std::to_string(value);
  }
  return out + ']';
}

const char* Bool(bool value) { return value ? "true" : "false"; }

int RequestedOutput(const io::Json& req, const ChatPlanConfig& config) {
  const int fallback =
      config.client_limits
          ? std::min(config.default_max_tokens,
                     static_cast<int>(config.client_limits->max_output_tokens))
          : config.default_max_tokens;
  return static_cast<int>(
      req.GetInt("max_completion_tokens", req.GetInt("max_tokens", fallback)));
}

std::string OutputSource(const io::Json& req) {
  for (const char* key : {"max_completion_tokens", "max_tokens"})
    if (const auto* value = req.Find(key); value && !value->IsNull())
      return key;
  return "server_default";
}

Status Fail(ChatPlanError* error, const std::string& code,
            const std::string& param, const std::string& message) {
  *error = {code, param};
  return Status::Fail(message);
}
}  // namespace

Status PlanChatRequest(const io::Json& req, const ChatPlanConfig& config,
                       ChatRequestPlan* plan, ChatPlanError* error) {
  *plan = {};
  *error = {};
  if (HasLegacyTools(req)) {
    LegacyToolRequest normalized;
    auto status = NormalizeLegacyTools(req, &normalized, &error->param);
    if (!status) return status;
    status = PlanChatRequest(normalized.request, config, plan, error);
    if (!status) LegacyToolErrorParam(req, &error->param);
    plan->legacy_output = normalized.legacy_output;
    plan->normalized_request = std::move(normalized.request);
    return status;
  }
  auto status = ValidateChatContract(req, config.model, config.allow_media,
                                     &error->param);
  if (!status) return status;
  status = ParseSamplingOptions(req, &plan->sampling, &error->param);
  if (!status) return status;
  status = ParsePredictionOptions(req, plan->sampling, &plan->prediction,
                                  &error->param);
  if (!status) return status;
  if (const auto* biases = req.Find("logit_bias");
      biases && biases->IsObject() && config.token_vocab > 0) {
    for (const auto& [key, bias] : biases->object) {
      (void)bias;
      int32_t token = -1;
      std::from_chars(key.data(), key.data() + key.size(), token);
      if (token >= config.token_vocab)
        return Fail(error, "invalid_request", InputParamPath("logit_bias", key),
                    "logit_bias token outside served vocabulary");
    }
  }
  if (config.client_limits &&
      static_cast<int64_t>(RequestedOutput(req, config)) *
              plan->sampling.choices >
          static_cast<int>(config.client_limits->max_output_tokens))
    return Fail(error, "client_output_limit_exceeded", OutputSource(req),
                "requested output exceeds client token limit");
  status = PlanOutputConstraints(req, &plan->constraints, &error->param);
  if (!status)
    return Fail(error, "invalid_output_constraint", error->param,
                status.message());
  if (plan->constraints.active && !config.structured)
    return Fail(error, "structured_backend_unavailable", "",
                "structured decoding backend is not built");
  status =
      ParseTextStop(req, plan->constraints.active, &plan->stops, &error->param);
  if (!status) return status;
  if (!req.GetString("prefix_cache_scope").empty() && !config.prefix_cache)
    return Fail(error, "invalid_request", "prefix_cache_scope",
                "prefix cache is not enabled");
  if (!req.GetString("session_id").empty() && !config.session_cache)
    return Fail(error, "invalid_request", "session_id",
                "session cache is not enabled");
  return Status();
}

int ChatOutputLimit(const io::Json& req, const ChatPlanConfig& config) {
  return std::min(RequestedOutput(req, config), config.max_len);
}

Status PlanChatBudget(const io::Json& req, const ChatPlanConfig& config,
                      size_t tokens, ChatPromptBudget* budget,
                      ChatPlanError* error) {
  *budget = {};
  *error = {};
  const auto input = req.Has("messages") ? "messages" : "prompt";
  if (config.client_limits && tokens > config.client_limits->max_prompt_tokens)
    return Fail(error, "client_prompt_limit_exceeded", input,
                "encoded prompt exceeds client token limit");
  SamplingOptions sampling;
  auto sampling_status = ParseSamplingOptions(req, &sampling, &error->param);
  if (!sampling_status) return sampling_status;
  if (config.client_limits &&
      static_cast<int64_t>(RequestedOutput(req, config)) * sampling.choices >
          static_cast<int>(config.client_limits->max_output_tokens))
    return Fail(error, "client_output_limit_exceeded", OutputSource(req),
                "requested output exceeds client token limit");
  if (!tokens || tokens >= static_cast<size_t>(config.max_len))
    return Fail(error, "context_length_exceeded", input,
                "prompt tokens must be positive and below context capacity");
  ChatPromptBudget planned;
  planned.output_source = OutputSource(req);
  planned.requested_output = RequestedOutput(req, config);
  planned.capped_output = ChatOutputLimit(req, config);
  planned.available_output = std::min(
      planned.capped_output, config.max_len - static_cast<int>(tokens));
  if (!config.capacities.empty()) {
    planned.required_capacity = tokens + planned.capped_output;
    for (size_t slot = 0; slot < config.capacities.size(); ++slot)
      if (planned.required_capacity <=
          static_cast<size_t>(config.capacities[slot]))
        planned.eligible_slots.push_back(static_cast<int>(slot));
    if (planned.eligible_slots.empty())
      return Fail(error, "context_length_exceeded",
                  planned.output_source == "server_default"
                      ? input
                      : planned.output_source,
                  "input plus output budget exceeds slot capacity");
  } else {
    for (int slot = 0; slot < config.max_seq; ++slot)
      planned.eligible_slots.push_back(slot);
  }
  planned.total_output =
      static_cast<uint64_t>(planned.available_output) * sampling.choices;
  const auto prefix = ValidatePrefixSeedBoundary(
      static_cast<int>(req.GetInt("prefix_cache_write_tokens", 0)),
      config.checkpoints, tokens);
  if (!prefix)
    return Fail(error, "invalid_request", "prefix_cache_write_tokens",
                prefix.message());
  *budget = std::move(planned);
  return Status();
}

Status EncodeChatPrompt(const io::Json& req, const ChatPlanConfig& config,
                        std::string_view prompt, const PromptEncoder& encode,
                        std::vector<uint32_t>* tokens, ChatPromptBudget* budget,
                        ChatPlanError* error) {
  tokens->clear();
  *budget = {};
  *error = {};
  const auto encoded = encode(prompt, tokens);
  if (!encoded) {
    tokens->clear();
    return Fail(error, "invalid_request",
                req.Has("messages") ? "messages" : "prompt",
                "encode failed: " + encoded.message());
  }
  auto status = PlanChatBudget(req, config, tokens->size(), budget, error);
  SamplingOptions sampling;
  PredictionOptions prediction;
  if (status) status = ParseSamplingOptions(req, &sampling, &error->param);
  if (status)
    status = ParsePredictionOptions(req, sampling, &prediction, &error->param);
  if (!status || !prediction.enabled) return status;
  std::vector<uint32_t> predicted;
  status = encode(prediction.content, &predicted);
  if (!status)
    return Fail(error, "invalid_request", "prediction.content",
                "prediction encode failed: " + status.message());
  if (predicted.size() > static_cast<size_t>(budget->available_output))
    return Fail(error, "context_length_exceeded", "prediction.content",
                "prediction tokens exceed available completion budget");
  for (const auto id : predicted) {
    if (id > static_cast<uint32_t>(std::numeric_limits<int32_t>::max()) ||
        (config.token_vocab && id >= static_cast<uint32_t>(config.token_vocab)))
      return Fail(error, "invalid_request", "prediction.content",
                  "prediction token outside served vocabulary");
    budget->prediction_tokens.push_back(static_cast<int32_t>(id));
  }
  return Status();
}

Status ValidatePreflightEnvelope(const io::Json& body, ChatPlanError* error) {
  *error = {};
  if (!body.IsObject())
    return Fail(error, "invalid_request", "body", "expected JSON object");
  for (const auto& [key, value] : body.object) {
    if (key == "request") continue;
    if (key != "include_prompt" && key != "include_token_ids")
      return Fail(error, "invalid_request", key, "unsupported preflight field");
    if (!value.IsBool())
      return Fail(error, "invalid_request", key,
                  "preview option must be boolean");
  }
  const auto* request = body.Find("request");
  if (!request || !request->IsObject())
    return Fail(error, "invalid_request", "request",
                "request must be an object");
  return Status();
}

Status ChatPreflightJson(const io::Json& envelope, const ChatPlanConfig& config,
                         const ChatRequestPlan& plan,
                         const ChatPromptBudget& budget,
                         const std::string& prompt,
                         const std::vector<uint32_t>& tokens,
                         std::string* output, ChatPlanError* error,
                         const vision::MediaPlan* media) {
  output->clear();
  const bool explicit_slots = !config.capacities.empty();
  std::string out =
      R"({"object":"q4t.chat.preflight","profile":)" +
      Quote(std::string(kPreflightProfile)) + R"(,"model":)" +
      Quote(config.model) + R"(,"protocol_source_sha256":)" +
      Quote(identity::kProtocol) + R"(,"prompt_tokens":)" +
      std::to_string(tokens.size()) + R"(,"prompt_bytes":)" +
      std::to_string(prompt.size()) + R"(,"max_context_tokens":)" +
      std::to_string(config.max_len) + R"(,"prefill_chunk_tokens":)" +
      std::to_string(config.max_prefill) + R"(,"prefill_chunks":)" +
      std::to_string((tokens.size() + config.max_prefill - 1) /
                     config.max_prefill) +
      R"(,"output":{"source":)" + Quote(budget.output_source) +
      R"(,"requested_tokens":)" + std::to_string(budget.requested_output) +
      R"(,"capped_tokens":)" + std::to_string(budget.capped_output) +
      R"(,"available_tokens":)" + std::to_string(budget.available_output) +
      R"(,"choices":)" + std::to_string(plan.sampling.choices) +
      R"(,"total_available_tokens":)" + std::to_string(budget.total_output) +
      R"(,"prediction_tokens":)" +
      std::to_string(budget.prediction_tokens.size()) +
      R"(,"counts_reasoning_and_forced_tokens":true},"sampling":{"temperature":)" +
      std::to_string(plan.sampling.temperature) + R"(,"top_p":)" +
      std::to_string(plan.sampling.top_p) + R"(,"path":)" +
      Quote(plan.prediction.enabled ? "target_prediction_verify"
            : plan.sampling.PlainGreedy() ? "greedy_mtp_or_scheduler"
                                        : "target_logits_host_sampler") +
      R"(,"seed":)" +
      (plan.sampling.seed ? std::to_string(*plan.sampling.seed) : "null") +
      R"(,"logprobs":)" + Bool(plan.sampling.logprobs) +
      R"(},"admission":{"policy":)" +
      Quote(explicit_slots ? "explicit_full_budget" : "uniform_context_clip") +
      R"(,"required_capacity":)" + std::to_string(budget.required_capacity) +
      R"(,"eligible_slots":)" + Array(budget.eligible_slots) +
      R"(,"slot_reserved":false,"availability_checked":false},)"
      R"("checks":{"input":true,"template":true,"tokenizer":true,)"
      R"("budget":true,"structured_requested":)" +
      Bool(plan.constraints.active) +
      R"(,"grammar_compiled":false,"cache_accessed":false,)"
      R"("request_registered":false,"gpu_executed":false},)"
      R"("execution_guaranteed":false,"unvalidated":["runtime_health",)"
      R"("admission_and_deadline","request_id_and_session_leases",)"
      R"("grammar_compilation","thinking_boundary_tokens",)"
      R"("cache_hits","generation_and_output_validation"])";
  if (media && media->item_count) {
    out += R"(,"media":{"items":)" + std::to_string(media->item_count) +
           R"(,"visual_tokens":)" + std::to_string(media->merged_tokens) +
           R"(,"headers_and_geometry_checked":true,"pixels_decoded":false, )"
           R"("prompt_preview":"one_placeholder_per_temporal_group",)"
           R"("token_ids":"expanded_for_inference",)"
           R"("execution_and_quality_validated":false})";
  }
  if (config.client_limits) {
    out += R"(,"client_limits":{"max_prompt_tokens":)" +
           std::to_string(config.client_limits->max_prompt_tokens) +
           R"(,"max_output_tokens":)" +
           std::to_string(config.client_limits->max_output_tokens) +
           R"(,"requests_per_minute":)" +
           std::to_string(config.client_limits->requests_per_minute) +
           R"(,"tokens_per_minute":)" +
           std::to_string(config.client_limits->tokens_per_minute) +
           R"(,"rate_quota_reserved":false})";
  }
  // Compute the escaped size before allocating it. The optional preview has
  // a hard response bound, not silent truncation or an unbounded token array.
  size_t additional = 2;
  if (envelope.GetBool("include_prompt")) {
    additional += 12;
    for (const unsigned char c : prompt)
      additional += c < 0x20 ? 6 : (c == '"' || c == '\\' ? 2 : 1);
  }
  if (envelope.GetBool("include_token_ids"))
    additional += 16 + tokens.size() * 11;
  if (additional > kPreflightResponseBytes ||
      out.size() > kPreflightResponseBytes - additional)
    return Fail(error, "preflight_response_too_large",
                envelope.GetBool("include_prompt") ? "include_prompt"
                                                   : "include_token_ids",
                "preview exceeds the 8 MiB response budget; omit previews");
  if (envelope.GetBool("include_prompt"))
    out += R"(,"prompt":)" + Quote(prompt);
  if (envelope.GetBool("include_token_ids"))
    out += R"(,"token_ids":)" + Array(tokens);
  // The preflight never echoes request_id, cache capabilities or user metadata.
  *output = std::move(out) + '}';
  return Status();
}

std::string EffectiveServeConfigJson(const ServerOptions& requested,
                                     const ChatPlanConfig& config,
                                     bool budget_enabled, bool mtp_enabled,
                                     int business_connections,
                                     const StartupMemoryChecks* startup) {
  const ServerOptions defaults;
  const auto source = [&](std::string_view key, bool default_value) {
    if (std::find(requested.supplied_options.begin(),
                  requested.supplied_options.end(),
                  key) != requested.supplied_options.end())
      return "cli";
    return default_value ? "builtin_default" : "server_options";
  };
  const auto number = [&](std::string_view key, int input, int fallback,
                          int value, const char* resolution) {
    return std::string(R"({"requested":)") + std::to_string(input) +
           R"(,"effective":)" + std::to_string(value) + R"(,"source":)" +
           Quote(source(key, input == fallback)) + R"(,"resolution":)" +
           Quote(resolution) + '}';
  };
  auto capacities = config.capacities;
  if (capacities.empty()) capacities.assign(config.max_seq, config.max_len);
  const text::TokenizerLimits tokenizer;
  const int public_handlers = business_connections + kOperationsConnections;
  const int operations_handlers =
      requested.operations_port ? kOperationsConnections : 0;
  return R"({"object":"q4t.serve.config","profile":"q4t-config-v1",)"
         R"("observability_version":5,)"
         R"("protocol_source_sha256":)" +
         Quote(identity::kProtocol) + R"(,"model":)" + Quote(config.model) +
         R"(,"client_policy":{"profile":"q4t-gateway-clients-v1","enabled":)" +
         Bool(config.client_count != 0) + R"(,"sha256":)" +
         Quote(config.client_policy_sha256) + R"(,"clients":)" +
         std::to_string(config.client_count) +
         R"(,"namespace":"client_id","credential_values_redacted":true,)"
         R"("mutable":false,"per_client_ram_limit":false},)"
         R"("client_governance":{"profile":"q4t-client-governance-v1",)"
         R"("policy_version":)" +
         std::to_string(config.client_policy_version) + R"(,"fair_queue":)" +
         Bool(config.client_fair_queue) +
         R"(,"window_seconds":60,"window":"fixed_monotonic",)"
         R"("zero_disables":true,"scope":"single_process",)"
         R"("restart":"reset","preflight_charged":false,)"
         R"("reservation":"prompt_plus_available_output",)"
         R"("settlement":"logical_tokens_in_completion_window",)"
         R"("pending_survives_window":true,)"
         R"("fairness_scope":"prepared_slot_admission",)"
         R"("preemption":false,"latency_guarantee":false},)"
         R"("listeners":{"public":{"host":)" +
         Quote(requested.host) + R"(,"port":)" +
         std::to_string(requested.port) +
         R"(},"operations":{"host":"127.0.0.1","port":)" +
         std::to_string(requested.operations_port) + R"(,"enabled":)" +
         Bool(requested.operations_port != 0) + R"(,"source":)" +
         Quote(source("--operations-port", requested.operations_port == 0)) +
         R"(,"unclassified_shares_operations_total":true}},)"
         R"("max_context_tokens":)" +
         number("--max-len", requested.max_len, defaults.max_len,
                config.max_len,
                !config.capacities.empty() ? "explicit_slots"
                : budget_enabled           ? "startup_memory_budget"
                : requested.max_len        ? "requested"
                                           : "model_config_default") +
         R"(,"max_sequences":)" +
         number("--max-seq", requested.max_seq, defaults.max_seq,
                config.max_seq,
                budget_enabled ? "startup_memory_budget" : "requested") +
         R"(,"prefill_chunk_tokens":)" +
         number("--max-prefill", requested.max_prefill, defaults.max_prefill,
                config.max_prefill,
                requested.max_prefill ? "requested" : "model_config_default") +
         R"(,"default_output_tokens":)" +
         number("--max-tokens", requested.max_tokens, defaults.max_tokens,
                config.default_max_tokens,
                "request_override_then_context_cap") +
         R"(,"request_deadline_ms":)" +
         number("--request-deadline-ms", requested.request_deadline_ms,
                defaults.request_deadline_ms, config.request_deadline_ms,
                "request_override") +
         R"(,"max_inflight_requests":)" +
         number("--max-inflight-requests", requested.max_inflight_requests,
                defaults.max_inflight_requests, config.admission.inflight,
                requested.max_inflight_requests ? "requested"
                                                : "effective_slots_default") +
         R"(,"max_queued_requests":)" +
         number("--max-queued-requests", requested.max_queued_requests,
                defaults.max_queued_requests, config.admission.queued,
                requested.max_queued_requests >= 0
                    ? "requested"
                    : "effective_admission_default") +
         R"(,"slots":{"capacities":)" + Array(capacities) + R"(,"policy":)" +
         Quote(config.capacities.empty() ? "uniform_context_clip"
                                         : "explicit_full_budget") +
         R"(,"availability_endpoint":"/metrics","reservation":false},)"
         R"("runtime":{"mtp_requested":)" +
         Bool(!requested.no_mtp) + R"(,"mtp_enabled":)" + Bool(mtp_enabled) +
         R"(,"mtp_k":)" +
         (mtp_enabled ? std::to_string(requested.mtp_k) : "null") +
         R"(,"media_enabled":)" + Bool(config.allow_media) +
         R"(,"media_limits":)" +
         MediaLimitsJson(config.media_attention_shared_bytes) +
         R"(,"structured_backend":)" + Bool(config.structured) +
         R"(,"session_cache":)" + Bool(config.session_cache) +
         R"(,"prefix_cache":)" + Bool(config.prefix_cache) +
         R"(,"prefix_checkpoints":)" + Array(config.checkpoints) +
         R"(,"memory_budget_enabled":)" + Bool(budget_enabled) +
         R"(,"whole_server_ram_limit":false},"startup_memory":{"enabled":)" +
         Bool(budget_enabled && requested.startup_memory_headroom_mib != 0) +
         R"(,"headroom_mib":)" +
         number("--startup-memory-headroom-mib",
                requested.startup_memory_headroom_mib,
                defaults.startup_memory_headroom_mib,
                budget_enabled ? requested.startup_memory_headroom_mib : 0,
                budget_enabled ? "zero_disables" : "budget_disabled") +
         R"(,"source":"linux_global_mem_available",)"
         R"("estimate":"allocation_budget_peak",)"
         R"("failure_policy":"stop_startup",)"
         R"("snapshot_is_reservation":false,"hard_limit":false,"checks":)" +
         (startup ? StartupMemoryChecksJson(*startup) : "null") +
         R"(},"cuda_allocation_quota":{"profile":)"
         R"("q4t-linked-cuda-quota-v1","enabled":)" +
         Bool(config.cuda_quota_limit_bytes != 0) + R"(,"requested_mib":)" +
         (requested.cuda_allocation_limit_mib < 0
              ? Quote("auto")
              : std::to_string(requested.cuda_allocation_limit_mib)) +
         R"(,"effective_limit_bytes":)" +
         std::to_string(config.cuda_quota_limit_bytes) + R"(,"source":)" +
         Quote(source("--cuda-allocation-limit-mib",
                      requested.cuda_allocation_limit_mib == -1)) +
         R"(,"resolution":)" +
         Quote(requested.cuda_allocation_limit_mib < 0
                   ? "mem_fraction_times_mem_total"
               : requested.cuda_allocation_limit_mib == 0
                   ? "explicitly_disabled"
                   : "requested_mib") +
         R"(,"scope":"process_requested_cuda_bytes","allocation_entries":16384,)"
         R"("stream_entries":256,"no_budget_bypasses_quota":false,)"
         R"("external_library_calls_covered":false,"physical_ram_limit":false,)"
         R"("reconfiguration":"new_process","async_credit":"successful_sync",)"
         R"("excluded":["host_heap","file_cache","driver",)"
         R"("external_library_allocations","cuda_pool_retained"]},)"
         R"("memory_admission":{"enabled":)" +
         Bool(requested.min_available_memory_mib != 0) +
         R"(,"source":"linux_global_mem_available","min_available_mib":)" +
         number("--min-available-memory-mib",
                requested.min_available_memory_mib,
                defaults.min_available_memory_mib,
                requested.min_available_memory_mib, "zero_disables") +
         R"(,"recovery_margin_mib":)" +
         number("--memory-recovery-margin-mib",
                requested.memory_recovery_margin_mib,
                defaults.memory_recovery_margin_mib,
                requested.min_available_memory_mib
                    ? requested.memory_recovery_margin_mib
                    : 0,
                "ignored_when_disabled") +
         R"(,"recovery_threshold_mib":)" +
         std::to_string(requested.min_available_memory_mib
                            ? requested.min_available_memory_mib +
                                  requested.memory_recovery_margin_mib
                            : 0) +
         R"(,"sample_max_age_ms":250,"failure_policy":"reject_new_work",)"
         R"("active_generation":"continue","hard_limit":false},)"
         R"("limits":{"text_requests":)" +
         std::to_string(config.admission.inflight) + R"(,"queued_requests":)" +
         std::to_string(config.admission.queued) + R"(,"queue_policy":)" +
         Quote(config.client_fair_queue ? "client_round_robin_best_fit"
                                        : "capacity_fifo_best_fit") +
         ',' +
         R"("queue_order":"slot_admission_arrival",)"
         R"("queue_excludes_preparation":true)" +
         R"(,"preflight_shares_text_admission":true,"business_connections":)" +
         std::to_string(business_connections) + R"(,"control_connections":)" +
         std::to_string(kReservedControlConnections) +
         R"(,"observation_connections":)" +
         std::to_string(kReservedObservationConnections) +
         R"(,"public_connection_handlers":)" + std::to_string(public_handlers) +
         R"(,"operations_connection_handlers":)" +
         std::to_string(operations_handlers) + R"(,"connection_handlers":)" +
         std::to_string(public_handlers + operations_handlers) +
         R"(,"unclassified_shares_total":)" +
         Bool(requested.operations_port == 0) + R"(,"body_bytes":)" +
         std::to_string(kChatBodyBytes) + R"(,"aggregate_body_bytes":)" +
         std::to_string(kAggregateChatBodyBytes) +
         R"(,"tokenizer_input_bytes":)" +
         std::to_string(tokenizer.max_input_bytes) + R"(,"tokenizer_tokens":)" +
         std::to_string(tokenizer.max_tokens) +
         R"(,"preflight_response_bytes":)" +
         std::to_string(kPreflightResponseBytes) +
         R"(},"configuration_mutable":false,"paths_redacted":true})";
}

}  // namespace q4t::server
