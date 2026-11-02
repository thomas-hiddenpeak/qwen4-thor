#include "q4t/server/chat_plan.h"

#include <algorithm>

#include "q4t/server/chat_contract.h"
#include "q4t/server/chat_protocol_identity.h"
#include "q4t/server/http_request.h"
#include "q4t/server/http_response.h"
#include "q4t/server/prefix_cache_policy.h"
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
  return static_cast<int>(
      req.GetInt("max_completion_tokens",
                 req.GetInt("max_tokens", config.default_max_tokens)));
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
  auto status = ValidateChatContract(req, config.model, config.allow_media,
                                     &error->param);
  if (!status) return status;
  status = PlanOutputConstraints(req, &plan->constraints);
  if (!status)
    return Fail(error, "invalid_output_constraint", "", status.message());
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
  return PlanChatBudget(req, config, tokens->size(), budget, error);
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
                         std::string* output, ChatPlanError* error) {
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
      R"(,"counts_reasoning_and_forced_tokens":true},"admission":{"policy":)" +
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
                                     int business_connections) {
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
  return R"({"object":"q4t.serve.config","profile":"q4t-config-v1",)"
         R"("protocol_source_sha256":)" +
         Quote(identity::kProtocol) + R"(,"model":)" + Quote(config.model) +
         R"(,"max_context_tokens":)" +
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
         R"(,"slots":{"capacities":)" + Array(capacities) + R"(,"policy":)" +
         Quote(config.capacities.empty() ? "uniform_context_clip"
                                         : "explicit_full_budget") +
         R"(,"availability_endpoint":"/metrics","reservation":false},)"
         R"("runtime":{"mtp_requested":)" +
         Bool(!requested.no_mtp) + R"(,"mtp_enabled":)" + Bool(mtp_enabled) +
         R"(,"mtp_k":)" +
         (mtp_enabled ? std::to_string(requested.mtp_k) : "null") +
         R"(,"media_enabled":)" + Bool(config.allow_media) +
         R"(,"structured_backend":)" + Bool(config.structured) +
         R"(,"session_cache":)" + Bool(config.session_cache) +
         R"(,"prefix_cache":)" + Bool(config.prefix_cache) +
         R"(,"prefix_checkpoints":)" + Array(config.checkpoints) +
         R"(,"memory_budget_enabled":)" + Bool(budget_enabled) +
         R"(,"whole_server_ram_limit":false},"limits":{"text_requests":)" +
         std::to_string(TextAdmissionLimit(config.max_seq)) +
         R"(,"preflight_shares_text_admission":true,"business_connections":)" +
         std::to_string(business_connections) + R"(,"control_connections":)" +
         std::to_string(kReservedControlConnections) + R"(,"body_bytes":)" +
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
