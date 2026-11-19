#include "q4t/server/realtime_chat_plan.h"

#include "../src/server/responses_json.h"
#include "q4t/server/output_schema.h"
#include "q4t/test.h"

namespace {
using q4t::Status;
using namespace q4t::server;
using namespace q4t::server::responses_detail;
Json Parse(std::string text) {
  Json result;
  if (!q4t::io::ParseJson(text, &result, true))
    throw std::runtime_error("Realtime planning fixture JSON");
  return result;
}
ChatPlanConfig Config() {
  ChatPlanConfig config;
  config.model = "q4t_cpu_fixture";
  config.structured = true;
  config.max_len = 8192;
  return config;
}
Json Message(std::string text = "hello") {
  return Object({{"type", String("message")},
                 {"role", String("user")},
                 {"content", Array({Object({{"type", String("input_text")},
                                            {"text", String(text)}})})}});
}
struct Fixture {
  ChatPlanConfig config = Config();
  Json settings = DefaultRealtimeChatSettings(config, "");
  RealtimeChatPlan plan;
  ChatPlanError error;
  bool Build(const Json& response = Object(),
             const Json& items = Array({Message()})) {
    const auto status =
        PlanRealtimeChat(settings, response, items, config, &plan, &error);
    if (!status)
      std::fprintf(stderr, "%s at %s: %s\n", error.code.c_str(),
                   error.param.c_str(), status.message().c_str());
    return status.ok();
  }
};
}  // namespace

Q4T_TEST(realtime_chat_defaults_and_per_response_override_are_effective) {
  Fixture f;
  Q4T_CHECK(f.Build(Parse(R"({"instructions":"use this instruction",
                            "max_output_tokens":29})")));
  const auto& request = f.plan.generation.generation_request;
  Q4T_CHECK(request.GetInt("max_completion_tokens") == 29);
  Q4T_CHECK(!request.GetBool("stream") && !request.GetBool("enable_thinking"));
  Q4T_CHECK(request.Find("messages")->array.front().GetString("content") ==
            "use this instruction");
  Q4T_CHECK(f.settings.GetString("instructions") ==
            "You are a helpful assistant.");
  Q4T_CHECK(f.Build(Object(), Array()) &&
            f.plan.generation.input.items.array.empty());
  return true;
}

Q4T_TEST(
    realtime_chat_inf_uses_model_and_client_limits_without_loosening_number) {
  Fixture f;
  ClientLimits limits;
  limits.max_output_tokens = 128;
  f.config.client_limits = &limits;
  Q4T_CHECK(f.Build() && f.plan.generation.generation_request.GetInt(
                             "max_completion_tokens") == 128);
  Q4T_CHECK(!f.Build(Parse(R"({"max_output_tokens":129})")) &&
            f.error.code == "client_output_limit_exceeded");
  f.config.client_limits = nullptr;
  for (auto value : {"0", "4097", "1.2", "null"})
    Q4T_CHECK(
        !f.Build(Parse(std::string("{\"max_output_tokens\":") + value + '}')));
  Json underflow;
  Q4T_CHECK(!q4t::io::ParseJson(R"({"max_output_tokens":1e-999})", &underflow,
                               true));
  return true;
}

Q4T_TEST(realtime_chat_native_function_schema_and_forced_choice_are_preserved) {
  Fixture f;
  Set(&f.settings, "tools", Parse(R"([{"name":"lookup","parameters":{
    "type":"object","properties":{"key":{"type":"string"}},
    "additionalProperties":true}}])"));
  Q4T_CHECK(f.Build(Parse(R"({"tool_choice":{"type":"function","name":"lookup"},
                           "parallel_tool_calls":false})")));
  const auto& request = f.plan.generation.generation_request;
  const auto* function = request.Find("tools")->array[0].Find("function");
  Q4T_CHECK(function && !function->GetBool("strict", true));
  Q4T_CHECK(function->Find("parameters")->GetBool("additionalProperties"));
  Q4T_CHECK(request.Find("tool_choice")->Find("function")->GetString("name") ==
                "lookup" &&
            !request.GetBool("parallel_tool_calls", true));
  return true;
}

Q4T_TEST(realtime_chat_tool_result_history_keeps_actual_content_and_ids) {
  Fixture f;
  const auto items = Parse(R"([
    {"type":"function_call","object":"realtime.item","id":"i1",
     "name":"lookup","call_id":"c1","arguments":"{}"},
    {"type":"function_call_output","call_id":"c1","output":"actual result"}
  ])");
  Q4T_CHECK(f.Build(Object(), items));
  const auto& messages =
      f.plan.generation.generation_request.Find("messages")->array;
  Q4T_CHECK(messages.size() == 3);
  Q4T_CHECK(messages[1].Find("tool_calls")->array[0].GetString("id") == "c1");
  Q4T_CHECK(messages[2].GetString("content") == "actual result" &&
            messages[2].GetString("tool_call_id") == "c1");
  auto missing = items;
  missing.array.pop_back();
  Q4T_CHECK(!f.Build(Object(), missing));
  auto orphan = items;
  orphan.array.erase(orphan.array.begin());
  Q4T_CHECK(!f.Build(Object(), orphan));
  return true;
}

Q4T_TEST(realtime_chat_query_identity_and_unknown_fields_fail_closed) {
  Fixture f;
  Json settings;
  Q4T_CHECK(ParseRealtimeChatTarget("/v1/realtime?model=q4t_cpu_fixture",
                                    f.config, &settings, &f.error));
  Q4T_CHECK(settings.GetString("model") == f.config.model);
  for (auto target : {"/v1/realtime?model=x&model=y", "/v1/realtime?model=%ff",
                      "/v1/realtime?extra=1", "/v1/realtime?model=",
                      "/v1/realtime?intent=wrong", "/wrong"})
    Q4T_CHECK(!ParseRealtimeChatTarget(target, f.config, &settings, &f.error));
  Set(&f.settings, "model", String("unregistered_model"));
  Q4T_CHECK(!f.Build() && f.error.code == "model_not_found");
  return true;
}

Q4T_TEST(realtime_chat_invalid_metadata_or_unconnected_work_is_never_ignored) {
  Fixture f;
  Q4T_CHECK(!f.Build(Object(
      {{"metadata", Object({{"key", String(std::string(513, 'x'))}})}})));
  Q4T_CHECK(f.error.param == "response.metadata");
  for (
      const auto* response :
      {R"({"output_modalities":["audio"]})",
       R"({"prompt":{"id":"pmpt_fixture"}})",
       R"({"tools":[{"type":"mcp","server_label":"x","server_url":"https://example.invalid"}]})"}) {
    Q4T_CHECK(!f.Build(Parse(response)));
    Q4T_CHECK(f.error.code == "realtime_processor_unavailable");
  }
  auto before = f.plan.generation.generation_request;
  Set(&f.settings, "instructions", String(std::string(16ULL << 20, 'x')));
  Q4T_CHECK(!f.Build() && OutputJson(before) ==
                              OutputJson(f.plan.generation.generation_request));
  return true;
}

Q4T_TEST(realtime_chat_context_cut_preserves_function_call_result_group) {
  Fixture f;
  f.config.max_len = 400;
  const auto items = Parse(R"([
    {"type":"message","role":"user","content":[{"type":"input_text","text":"old"}]},
    {"type":"function_call","name":"lookup","call_id":"c1","arguments":"{}"},
    {"type":"function_call_output","call_id":"c1","output":"old result"},
    {"type":"message","role":"user","content":[{"type":"input_text","text":"new"}]}
  ])");
  Q4T_CHECK(f.Build(Object({{"max_output_tokens", Number(20)}}), items));
  size_t checked = 0;
  Q4T_CHECK(FitRealtimeChatContext(
      f.config,
      [&](const ResponsesGenerationPlan& plan, size_t* tokens, ChatPlanError*) {
        ++checked;
        *tokens = 100 + plan.input.items.array.size() * 100;
        return Status{};
      },
      &f.plan, &f.error));
  Q4T_CHECK(checked >= 3 && f.plan.generation.truncated_items == 3);
  Q4T_CHECK(f.plan.generation.input.items.array.size() == 1);
  Q4T_CHECK(f.plan.generation.input.items.array[0]
                .Find("content")
                ->array[0]
                .GetString("text") == "new");
  return true;
}

Q4T_TEST(realtime_chat_retention_applies_only_after_actual_threshold_crossed) {
  Fixture f;
  f.config.max_len = 500;
  Set(&f.settings, "truncation", Parse(R"({"type":"retention_ratio",
      "retention_ratio":0.5,"token_limits":{"post_instructions":200}})"));
  const auto response = Object({{"max_output_tokens", Number(20)}});
  auto measure = [](const ResponsesGenerationPlan& plan, size_t* tokens,
                    ChatPlanError*) {
    *tokens = 100 + plan.input.items.array.size() * 100;
    return Status{};
  };
  Q4T_CHECK(f.Build(response, Array({Message("one"), Message("two")})));
  Q4T_CHECK(FitRealtimeChatContext(f.config, measure, &f.plan, &f.error));
  Q4T_CHECK(f.plan.generation.truncated_items == 0);
  Q4T_CHECK(f.Build(response,
                    Array({Message("one"), Message("two"), Message("three")})));
  Q4T_CHECK(FitRealtimeChatContext(f.config, measure, &f.plan, &f.error));
  Q4T_CHECK(f.plan.generation.truncated_items == 2);
  return true;
}

Q4T_TEST(
    realtime_chat_disabled_truncation_and_noncontext_errors_do_not_drop_input) {
  Fixture f;
  Q4T_CHECK(f.Build());
  size_t calls = 0;
  Q4T_CHECK(!FitRealtimeChatContext(
      f.config,
      [&](const ResponsesGenerationPlan&, size_t*, ChatPlanError* error) {
        ++calls;
        *error = {"client_tpm_exhausted", "input", 429};
        return Status::Fail("Actual quota failure fixture");
      },
      &f.plan, &f.error));
  Q4T_CHECK(calls == 1 && f.plan.generation.truncated_items == 0 &&
            f.error.code == "client_tpm_exhausted");
  Set(&f.settings, "truncation", String("disabled"));
  Q4T_CHECK(f.Build());
  Q4T_CHECK(!FitRealtimeChatContext(
      f.config,
      [](const ResponsesGenerationPlan&, size_t* tokens, ChatPlanError*) {
        *tokens = 10000;
        return Status{};
      },
      &f.plan, &f.error));
  Q4T_CHECK(f.error.code == "context_length_exceeded" &&
            f.plan.generation.truncated_items == 0);
  return true;
}

Q4T_TEST(realtime_chat_retention_bounds_and_model_effort_are_validated) {
  Fixture f;
  Q4T_CHECK(f.Build(Parse(R"({"reasoning":{"effort":"high"}})")));
  Q4T_CHECK(f.plan.generation.generation_request.GetBool("enable_thinking"));
  for (
      auto raw :
      {R"({"type":"retention_ratio","retention_ratio":1.01})",
       R"({"type":"retention_ratio","retention_ratio":-0.1})",
       R"({"type":"retention_ratio","retention_ratio":0.8,"token_limits":{"post_instructions":1.5}})"}) {
    Set(&f.settings, "truncation", Parse(raw));
    Q4T_CHECK(!f.Build());
  }
  return true;
}

Q4T_TEST(realtime_chat_empty_context_has_ephemeral_creation_cue) {
  Fixture f;
  for (const auto* instruction : {"Compute 6 * 7. Reply only 42.",
                                  "Reply only with the word blue."}) {
    const auto response = Object({{"instructions", String(instruction)}});
    Q4T_CHECK(f.Build(response, Array()));
    const auto& generation = f.plan.generation;
    Q4T_CHECK(generation.input.items.array.empty() &&
              generation.input_source_indices.empty());
    Q4T_CHECK(generation.input.messages.array.size() == 1 &&
              generation.input.messages.array[0].GetString("content") ==
                  instruction);
    const auto& request = generation.chat.Request(generation.generation_request);
    const auto& messages = request.Find("messages")->array;
    Q4T_CHECK(messages.size() == 2 &&
              messages[0].GetString("content") == instruction &&
              messages[1].GetString("role") == "user" &&
              messages[1].GetString("content") == instruction);
  }
  return true;
}

Q4T_TEST(realtime_chat_creation_cue_does_not_change_existing_items) {
  Fixture f;
  Q4T_CHECK(f.Build());
  const auto& generation = f.plan.generation;
  Q4T_CHECK(generation.input.items.array.size() == 1 &&
            generation.input_source_indices == std::vector<size_t>{0});
  const auto& messages = generation.generation_request.Find("messages")->array;
  Q4T_CHECK(messages.size() == 2 &&
            messages[1].GetString("role") == "user" &&
            OutputJson(messages[1]) ==
                OutputJson(generation.input.messages.array[1]));
  return true;
}

Q4T_TEST(realtime_chat_creation_cue_is_measured_and_never_truncated_as_history) {
  Fixture f;
  f.config.max_len = 100;
  Q4T_CHECK(f.Build(Object({{"max_output_tokens", Number(20)}}), Array()));
  size_t calls = 0;
  Q4T_CHECK(!FitRealtimeChatContext(
      f.config,
      [&](const ResponsesGenerationPlan& plan, size_t* count, ChatPlanError*) {
        ++calls;
        const auto& request = plan.chat.Request(plan.generation_request);
        *count = request.Find("messages")->array.size() * 45;
        return Status{};
      },
      &f.plan, &f.error));
  Q4T_CHECK(calls == 2 && f.error.code == "context_length_exceeded" &&
            f.plan.generation.input.items.array.empty() &&
            f.plan.generation.truncated_items == 0);
  return true;
}
