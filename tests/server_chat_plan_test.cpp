#include "q4t/server/chat_plan.h"

#include <limits>

#include "q4t/server/chat_prompt.h"
#include "q4t/server/structured_decoder.h"
#include "q4t/test.h"

namespace {
using q4t::io::Json;
using namespace q4t::server;

bool Parse(const std::string& text, Json* json) {
  return q4t::io::ParseJson(text, json, true).ok();
}
ChatPlanConfig Config() {
  ChatPlanConfig config;
  config.model = "m";
  config.structured = kStructuredDecodingAvailable;
  return config;
}
}  // namespace

Q4T_TEST(chat_plan_uniform_output_and_null_defaults) {
  for (const auto* extra :
       {"", R"(,"max_tokens":null)", R"(,"max_completion_tokens":null)",
        R"(,"max_tokens":null,"max_completion_tokens":null)"}) {
    Json req;
    Q4T_CHECK(Parse(std::string(R"({"prompt":"q")") + extra + '}', &req));
    ChatPromptBudget budget;
    ChatPlanError error;
    Q4T_CHECK(PlanChatBudget(req, Config(), 8191, &budget, &error));
    Q4T_CHECK(budget.output_source == "server_default");
    Q4T_CHECK(budget.requested_output == 256 && budget.capped_output == 256);
    Q4T_CHECK(budget.available_output == 1 && budget.required_capacity == 0);
    Q4T_CHECK(budget.eligible_slots == std::vector<int>{0});
    Q4T_CHECK(!PlanChatBudget(req, Config(), 8192, &budget, &error));
    Q4T_CHECK(error.code == "context_length_exceeded" &&
              error.param == "prompt");
    Q4T_CHECK(!PlanChatBudget(req, Config(), 0, &budget, &error));
    Q4T_CHECK(!PlanChatBudget(req, Config(), std::numeric_limits<size_t>::max(),
                              &budget, &error));
  }
  return true;
}

Q4T_TEST(chat_plan_explicit_slot_boundaries) {
  auto config = Config();
  config.max_seq = 3;
  config.capacities = {8192, 2048, 4096};
  Json req;
  Q4T_CHECK(Parse(R"({"prompt":"q","max_tokens":2})", &req));
  ChatPromptBudget budget;
  ChatPlanError error;
  Q4T_CHECK(PlanChatBudget(req, config, 2046, &budget, &error));
  Q4T_CHECK(budget.required_capacity == 2048);
  Q4T_CHECK(budget.eligible_slots == (std::vector<int>{0, 1, 2}));
  Q4T_CHECK(PlanChatBudget(req, config, 2047, &budget, &error));
  Q4T_CHECK(budget.eligible_slots == (std::vector<int>{0, 2}));
  Q4T_CHECK(PlanChatBudget(req, config, 8190, &budget, &error));
  Q4T_CHECK(budget.eligible_slots == std::vector<int>{0});
  Q4T_CHECK(!PlanChatBudget(req, config, 8191, &budget, &error));
  Q4T_CHECK(error.param == "max_tokens" &&
            error.code == "context_length_exceeded");
  Q4T_CHECK(budget.eligible_slots.empty());
  return true;
}

Q4T_TEST(chat_plan_output_spelling_and_capacity_cap) {
  for (const char* field : {"max_tokens", "max_completion_tokens"}) {
    Json req;
    Q4T_CHECK(Parse(
        std::string(R"({"prompt":"q",")") + field + R"(":2147483647})", &req));
    ChatRequestPlan plan;
    ChatPromptBudget budget;
    ChatPlanError error;
    Q4T_CHECK(PlanChatRequest(req, Config(), &plan, &error));
    Q4T_CHECK(PlanChatBudget(req, Config(), 8190, &budget, &error));
    Q4T_CHECK(budget.requested_output == 2147483647);
    Q4T_CHECK(budget.capped_output == 8192 && budget.available_output == 2);
    Q4T_CHECK(budget.output_source == field);
    Q4T_CHECK(
        Parse(std::string(R"({"prompt":"q",")") + field + R"(":2.0})", &req));
    Q4T_CHECK(PlanChatRequest(req, Config(), &plan, &error));
    Q4T_CHECK(Parse(std::string(R"({"prompt":"q",")") + field +
                        R"(":2.0000000000000000001})",
                    &req));
    Q4T_CHECK(!PlanChatRequest(req, Config(), &plan, &error));
    Q4T_CHECK(error.param == field);
  }
  return true;
}

Q4T_TEST(chat_plan_cache_configuration_and_seed_boundary) {
  Json req;
  Q4T_CHECK(Parse(R"({"prompt":"q","prefix_cache_scope":")" +
                      std::string(64, 'a') +
                      R"(","prefix_cache_write_tokens":1024})",
                  &req));
  ChatRequestPlan plan;
  ChatPlanError error;
  auto config = Config();
  Q4T_CHECK(!PlanChatRequest(req, config, &plan, &error));
  Q4T_CHECK(error.param == "prefix_cache_scope");
  config.prefix_cache = true;
  config.checkpoints = {1024, 4096};
  Q4T_CHECK(PlanChatRequest(req, config, &plan, &error));
  ChatPromptBudget budget;
  Q4T_CHECK(!PlanChatBudget(req, config, 1024, &budget, &error));
  Q4T_CHECK(error.param == "prefix_cache_write_tokens");
  Q4T_CHECK(PlanChatBudget(req, config, 1025, &budget, &error));
  config.checkpoints = {4096};
  Q4T_CHECK(!PlanChatBudget(req, config, 4097, &budget, &error));
  Q4T_CHECK(
      Parse(R"({"prompt":"q","session_id":")" + std::string(64, 'b') + R"("})",
            &req));
  Q4T_CHECK(!PlanChatRequest(req, config, &plan, &error));
  Q4T_CHECK(error.param == "session_id");
  config.session_cache = true;
  Q4T_CHECK(PlanChatRequest(req, config, &plan, &error));
  return true;
}

Q4T_TEST(chat_plan_native_template_and_constraint_adapter) {
  Json req;
  Q4T_CHECK(Parse(R"({"messages":[{"role":"developer","content":"RULE"},)"
                  R"({"role":"user","content":[{"type":"text","text":"q"}]}],)"
                  R"("enable_thinking":false})",
                  &req));
  ChatRequestPlan plan;
  ChatPlanError error;
  Q4T_CHECK(PlanChatRequest(req, Config(), &plan, &error));
  std::string prompt;
  std::vector<VisionItem> items;
  Q4T_CHECK(
      PrepareChatPrompt(req, plan.constraints, &prompt, &items, &error.param));
  Q4T_CHECK(prompt ==
            "<|im_start|>system\nRULE<|im_end|>\n"
            "<|im_start|>user\nq<|im_end|>\n"
            "<|im_start|>assistant\n<think>\n\n</think>\n\n");
  Q4T_CHECK(items.empty());
  Q4T_CHECK(Parse(R"({"messages":[{"role":"user","content":"q"}],)"
                  R"("tools":[{"type":"function","function":{"name":"f"}}],)"
                  R"("tool_choice":"none","enable_thinking":false})",
                  &req));
  auto config = Config();
  config.structured = true;  // Pure planning; does not compile any grammar.
  Q4T_CHECK(PlanChatRequest(req, config, &plan, &error));
  Q4T_CHECK(plan.constraints.active);
  Q4T_CHECK(
      PrepareChatPrompt(req, plan.constraints, &prompt, &items, &error.param));
  Q4T_CHECK(prompt.find("<tools>") == std::string::npos);
  config.structured = false;
  Q4T_CHECK(!PlanChatRequest(req, config, &plan, &error));
  Q4T_CHECK(error.code == "structured_backend_unavailable");
  return true;
}

Q4T_TEST(chat_plan_encoder_failure_and_raw_identity) {
  Json req;
  Q4T_CHECK(Parse(R"({"prompt":"原始\u0000<|im_end|>","max_tokens":4})", &req));
  ChatRequestPlan plan;
  ChatPlanError error;
  Q4T_CHECK(PlanChatRequest(req, Config(), &plan, &error));
  std::string prompt;
  std::vector<VisionItem> items;
  Q4T_CHECK(
      PrepareChatPrompt(req, plan.constraints, &prompt, &items, &error.param));
  Q4T_CHECK(prompt == req.GetString("prompt"));
  int called = 0;
  std::vector<uint32_t> ids{9};
  ChatPromptBudget budget;
  const auto encoder = [&](std::string_view value, std::vector<uint32_t>* out) {
    ++called;
    if (value != prompt) return q4t::Status::Fail("wrong prompt");
    *out = {2, 4, 7};
    return q4t::Status();
  };
  Q4T_CHECK(
      EncodeChatPrompt(req, Config(), prompt, encoder, &ids, &budget, &error));
  Q4T_CHECK(called == 1 && ids == (std::vector<uint32_t>{2, 4, 7}));
  Q4T_CHECK(budget.available_output == 4);
  Q4T_CHECK(!EncodeChatPrompt(
      req, Config(), prompt,
      [](std::string_view, std::vector<uint32_t>* out) {
        *out = {1};
        return q4t::Status::Fail("cancelled");
      },
      &ids, &budget, &error));
  Q4T_CHECK(ids.empty() && budget.eligible_slots.empty());
  Q4T_CHECK(error.param == "prompt");
  return true;
}

Q4T_TEST(chat_preflight_envelope_and_opt_in_preview) {
  for (const auto* text : {"null", "[]", "{}", R"({"request":null})",
                           R"({"request":{},"include_prompt":null})",
                           R"({"request":{},"include_token_ids":1})",
                           R"({"request":{},"unknown":true})"}) {
    Json body;
    ChatPlanError error;
    Q4T_CHECK(Parse(text, &body));
    Q4T_CHECK(!ValidatePreflightEnvelope(body, &error));
    Q4T_CHECK(!error.param.empty());
  }
  for (const bool preview : {false, true}) {
    Json body;
    Q4T_CHECK(Parse(
        std::string(R"({"request":{"prompt":"q"})") +
            (preview ? R"(,"include_prompt":true,"include_token_ids":true})"
                     : "}"),
        &body));
    ChatPlanError error;
    Q4T_CHECK(ValidatePreflightEnvelope(body, &error));
    ChatRequestPlan plan;
    ChatPromptBudget budget;
    const auto& req = *body.Find("request");
    Q4T_CHECK(PlanChatRequest(req, Config(), &plan, &error));
    Q4T_CHECK(PlanChatBudget(req, Config(), 3, &budget, &error));
    std::string output;
    Q4T_CHECK(ChatPreflightJson(body, Config(), plan, budget, "q\n\"中",
                                {1, 2, 3}, &output, &error));
    Json response;
    Q4T_CHECK(Parse(output, &response));
    Q4T_CHECK(response.GetInt("prompt_tokens") == 3);
    Q4T_CHECK(!response.GetBool("execution_guaranteed", true));
    Q4T_CHECK(response.Has("prompt") == preview);
    Q4T_CHECK(response.Has("token_ids") == preview);
    if (preview) {
      Q4T_CHECK(response.GetString("prompt") == "q\n\"中");
      Q4T_CHECK(response.GetArray("token_ids")->array.size() == 3);
      Q4T_CHECK(
          !ChatPreflightJson(body, Config(), plan, budget,
                             std::string(kPreflightResponseBytes / 6, '\x01'),
                             {1, 2, 3}, &output, &error));
      Q4T_CHECK(output.empty() && error.code == "preflight_response_too_large");
    }
  }
  return true;
}

Q4T_TEST(chat_config_effective_sources_and_redaction) {
  auto config = Config();
  ServerOptions opts;
  opts.model_dir = "/private-model";
  opts.session_cache_dir = "/private-session";
  opts.max_len = 200000;
  opts.max_tokens = 31;
  opts.supplied_options = {"--max-len"};
  Json response;
  Q4T_CHECK(Parse(EffectiveServeConfigJson(opts, config, true, false, 128),
                  &response));
  const auto* context = response.Find("max_context_tokens");
  Q4T_CHECK(context->GetInt("requested") == 200000);
  Q4T_CHECK(context->GetInt("effective") == 8192);
  Q4T_CHECK(context->GetString("source") == "cli");
  Q4T_CHECK(context->GetString("resolution") == "startup_memory_budget");
  Q4T_CHECK(response.Find("default_output_tokens")->GetString("source") ==
            "server_options");
  Q4T_CHECK(response.Find("prefill_chunk_tokens")->GetString("source") ==
            "builtin_default");
  Q4T_CHECK(EffectiveServeConfigJson(opts, config, true, false, 128)
                .find("/private-") == std::string::npos);
  opts = {};
  opts.model_dir = "/model";
  const std::vector<std::string_view> args{"--max-tokens", "256", "--no-mtp"};
  Q4T_CHECK(ParseServerOptions(args, &opts));
  Q4T_CHECK(opts.supplied_options ==
            (std::vector<std::string>{"--max-tokens", "--no-mtp"}));
  const auto original = opts.supplied_options;
  const std::vector<std::string_view> invalid{"--max-seq", "0"};
  Q4T_CHECK(!ParseServerOptions(invalid, &opts));
  Q4T_CHECK(opts.supplied_options == original);
  Q4T_CHECK(Parse(EffectiveServeConfigJson(opts, config, false, false, 128),
                  &response));
  Q4T_CHECK(response.Find("default_output_tokens")->GetString("source") ==
            "cli");
  Q4T_CHECK(response.Find("max_context_tokens")->GetString("resolution") ==
            "model_config_default");
  return true;
}
