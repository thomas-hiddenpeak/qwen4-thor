#include "q4t/server/realtime_chat_plan.h"
#include "q4t/server/chat_capabilities.h"
#include "q4t/server/realtime_conversation_session.h"

#include <algorithm>

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
PromptSnapshot Prompt(Json input) {
  return {PromptApi::kRealtime, "client:plan",
          Object({{"id", String("pmpt_fixture")}, {"version", String("1")}}),
          Object({{"input", std::move(input)}})};
}
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
  Q4T_CHECK(
      !q4t::io::ParseJson(R"({"max_output_tokens":1e-999})", &underflow, true));
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
      {R"({"prompt":{"id":"pmpt_fixture"}})",
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
  for (const auto* instruction :
       {"Compute 6 * 7. Reply only 42.", "Reply only with the word blue."}) {
    const auto response = Object({{"instructions", String(instruction)}});
    Q4T_CHECK(f.Build(response, Array()));
    const auto& generation = f.plan.generation;
    Q4T_CHECK(generation.input.items.array.empty() &&
              generation.input_source_indices.empty());
    Q4T_CHECK(generation.input.messages.array.size() == 1 &&
              generation.input.messages.array[0].GetString("content") ==
                  instruction);
    const auto& request =
        generation.chat.Request(generation.generation_request);
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
  Q4T_CHECK(messages.size() == 2 && messages[1].GetString("role") == "user" &&
            OutputJson(messages[1]) ==
                OutputJson(generation.input.messages.array[1]));
  return true;
}

Q4T_TEST(
    realtime_chat_creation_cue_is_measured_and_never_truncated_as_history) {
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

Q4T_TEST(realtime_chat_capabilities_do_not_require_transcription_backend) {
  const auto absent = Parse(ChatCapabilitiesJson("primary", false, false));
  const auto present = Parse(
      ChatCapabilitiesJson("primary", false, false, 0, nullptr, false, true));
  const auto* chat = absent.Find("realtime")->Find("conversation");
  Q4T_CHECK(chat && chat->GetBool("implemented") &&
            !chat->GetBool("requires_asr", true));
  auto text_only = *chat;
  auto with_asr = *present.Find("realtime")->Find("conversation");
  Q4T_CHECK(!text_only.Find("input_audio")->GetBool("configured") &&
            with_asr.Find("input_audio")->GetBool("configured") &&
            with_asr.GetInt("session_host_reservation_bytes") >
                text_only.GetInt("session_host_reservation_bytes"));
  for (auto* value : {&text_only, &with_asr})
    std::erase_if(value->object, [](const auto& field) {
      return field.first == "input_audio" ||
             field.first == "session_host_reservation_bytes";
    });
  Q4T_CHECK(OutputJson(text_only) == OutputJson(with_asr));
  Q4T_CHECK(chat->GetString("profile") == "q4t-native-realtime-chat-v2" &&
            chat->GetString("endpoint") == "/v1/realtime");
  Q4T_CHECK(chat->GetBool("function_calls") &&
            chat->GetBool("per_response_client_admission") &&
            !chat->GetBool("automatic_replay", true));
  return true;
}

Q4T_TEST(realtime_chat_capabilities_bind_real_storage_and_pending_processors) {
  const auto value = Parse(ChatCapabilitiesJson("primary", false, false));
  const auto* chat = value.Find("realtime")->Find("conversation");
  Q4T_CHECK(chat->GetInt("max_items") ==
                static_cast<int64_t>(RealtimeConversation::kMaximumItems) &&
            chat->GetInt("history_bytes") ==
                static_cast<int64_t>(RealtimeConversation::kMaximumBytes) &&
            chat->GetInt("session_host_reservation_bytes") ==
                static_cast<int64_t>(kRealtimeConversationHostBytes));
  Q4T_CHECK(
      OutputJson(*chat->Find("output_modalities")) == "[\"text\",\"audio\"]" &&
      OutputJson(*chat->Find("pending_execution")) == "[\"mcp\",\"tracing\"]");
  Q4T_CHECK(chat->Find("input_audio")->GetInt("idle_timeout_ms_min") == 5000 &&
            chat->Find("input_audio")->GetInt("idle_timeout_ms_max") == 30000 &&
            !chat->Find("input_audio")
                 ->Find("pipeline_recovery")
                 ->GetBool("model_restart", true));
  const auto* prompt = chat->Find("prompt");
  Q4T_CHECK(prompt && prompt->IsObject() &&
            prompt->GetString("profile") == "q4t-realtime-prompt-v1" &&
            prompt->GetString("scope") == "authenticated_client" &&
            prompt->GetString("snapshot") == "immutable_version" &&
            prompt->GetString("rpm") == "once_per_response" &&
            prompt->GetString("usage") == "includes_preparation" &&
            prompt->GetInt("maximum_retained_bytes") == (64LL << 20) &&
            prompt->GetInt("maximum_expansion_bytes") == (16LL << 20));
  Q4T_CHECK(chat->GetBool("cancel") && chat->GetBool("out_of_band") &&
            chat->GetBool("persistent_context_truncation") &&
            chat->GetString("usage") == "actual_tokens");
  return true;
}

Q4T_TEST(realtime_chat_prompt_prefix_is_real_input_before_public_history) {
  Fixture f;
  auto prompt = Prompt(Array({Message("private prefix")}));
  Set(&f.settings, "prompt", prompt.reference);
  Set(&f.settings, "instructions", String("template instruction"));
  const auto response = Object({{"instructions", String("one response")},
                                {"max_output_tokens", Number(31)}});
  Q4T_CHECK(PlanRealtimeChat(f.settings, response, Array({Message("public")}),
                             f.config, &f.plan, &f.error, &prompt));
  const auto& messages =
      f.plan.generation.generation_request.Find("messages")->array;
  Q4T_CHECK(messages.size() == 3 &&
            messages[0].GetString("content") == "one response");
  Q4T_CHECK(OutputJson(messages[1]).find("private prefix") !=
                std::string::npos &&
            OutputJson(messages[2]).find("public") != std::string::npos);
  Q4T_CHECK(f.plan.prompt_items == 1 &&
            f.plan.generation.input.items.array.size() == 2 &&
            RealtimeRetainedHistory(f.plan) == std::vector<size_t>{0});
  Q4T_CHECK(f.plan.generation.generation_request.GetInt(
                "max_completion_tokens") == 31);
  return true;
}

Q4T_TEST(
    realtime_chat_prompt_context_cut_projects_only_actual_history_indices) {
  for (const int max_len : {300, 200}) {
    Fixture f;
    f.config.max_len = max_len;
    auto prompt = Prompt(Array({Message("prefix one"), Message("prefix two")}));
    Set(&f.settings, "prompt", prompt.reference);
    Set(&f.settings, "max_output_tokens", Number(20));
    Q4T_CHECK(PlanRealtimeChat(f.settings, Object(),
                               Array({Message("old"), Message("current")}),
                               f.config, &f.plan, &f.error, &prompt));
    size_t first_count = 0;
    Q4T_CHECK(FitRealtimeChatContext(
        f.config,
        [&](const auto& generation, size_t* count, auto*) {
          *count = generation.input.items.array.size() * 100;
          if (!first_count) first_count = *count;
          return Status{};
        },
        &f.plan, &f.error));
    Q4T_CHECK(first_count == 400 && f.plan.generation.truncated_items >= 2);
    const auto expected =
        max_len == 300 ? std::vector<size_t>{0, 1} : std::vector<size_t>{1};
    Q4T_CHECK(RealtimeRetainedHistory(f.plan) == expected);
  }
  return true;
}

Q4T_TEST(realtime_chat_prompt_rejects_unbound_mismatched_or_extra_snapshots) {
  Fixture f;
  auto prompt = Prompt(Array({Message()}));
  Set(&f.settings, "prompt", prompt.reference);
  Q4T_CHECK(!PlanRealtimeChat(f.settings, Object(), Array(), f.config, &f.plan,
                              &f.error));
  Q4T_CHECK(f.error.code == "realtime_processor_unavailable");
  for (int mode = 0; mode < 4; ++mode) {
    auto wrong = prompt;
    if (mode == 0) wrong.scope.clear();
    if (mode == 1) wrong.api = PromptApi::kResponses;
    if (mode == 2) Set(&wrong.reference, "version", String("2"));
    if (mode == 3)
      Set(&wrong.reference, "variables", Object({{"x", String("x")}}));
    Q4T_CHECK(!PlanRealtimeChat(f.settings, Object(), Array(), f.config,
                                &f.plan, &f.error, &wrong));
    Q4T_CHECK(f.error.code == "prompt_snapshot_mismatch");
  }
  Q4T_CHECK(!PlanRealtimeChat(f.settings, Object({{"prompt", Json{}}}), Array(),
                              f.config, &f.plan, &f.error, &prompt));
  Q4T_CHECK(f.error.code == "prompt_snapshot_mismatch");
  return true;
}

Q4T_TEST(
    realtime_chat_prompt_cannot_bypass_client_budget_or_tool_history_checks) {
  Fixture f;
  ClientLimits limits;
  limits.max_output_tokens = 16;
  f.config.client_limits = &limits;
  auto prompt = Prompt(Array({Message("private")}));
  Set(&f.settings, "prompt", prompt.reference);
  Set(&f.settings, "max_output_tokens", Number(17));
  Q4T_CHECK(!PlanRealtimeChat(f.settings, Object(), Array(), f.config, &f.plan,
                              &f.error, &prompt));
  Q4T_CHECK(f.error.code == "client_output_limit_exceeded");
  Set(&f.settings, "max_output_tokens", Number(16));
  Set(&prompt.request, "input", Parse(R"([{"type":"function_call_output",
    "call_id":"missing","output":"unowned tool output"}])"));
  Q4T_CHECK(!PlanRealtimeChat(f.settings, Object(), Array(), f.config, &f.plan,
                              &f.error, &prompt));
  return true;
}

Q4T_TEST(realtime_chat_audio_output_preserves_native_preparation_plan) {
  Fixture f;
  Q4T_CHECK(f.Build(Parse(
      R"({"output_modalities":["audio"],"audio":{"output":{"format":{"type":"audio/pcmu"},"voice":"serena"}}})")));
  Q4T_CHECK(f.plan.speech.enabled && f.plan.speech.chat.text &&
            f.plan.speech.format.rate == 8000 &&
            f.plan.speech.chat.speech.voice == "serena");
  // Only the authenticated execution owner may attach the native observer.
  Q4T_CHECK(!f.plan.generation.chat.audio_output.enabled &&
            !f.plan.generation.generation_request.Has("audio"));
  const auto items = Parse(
      R"([{"type":"message","role":"assistant","content":[{"type":"output_audio","transcript":"retained speech"}]}])");
  Q4T_CHECK(f.Build(Object(), items));
  Q4T_CHECK(OutputJson(f.plan.generation.generation_request)
                .find("retained speech") != std::string::npos);
  auto raw = items;
  Set(&Field(raw.array[0], "content").array[0], "audio", String("AAAA"));
  Q4T_CHECK(!f.Build(Object(), raw));
  Q4T_CHECK(f.error.code == "realtime_processor_unavailable");
  return true;
}

Q4T_TEST(realtime_chat_native_input_requires_private_owner_and_real_history) {
  Fixture f;
  Set(&Field(f.settings, "audio"), "input",
      Parse(R"({"turn_detection":null,"transcription":null})"));
  Q4T_CHECK(!f.Build() && f.error.param == "audio.input");
  Q4T_CHECK(PlanRealtimeChat(f.settings, Object(),
                             Array({Message("actual ASR")}), f.config, &f.plan,
                             &f.error, nullptr, true));
  const auto* content =
      f.plan.generation.input.messages.array.back().GetArray("content");
  Q4T_CHECK(content && content->array.size() == 1 &&
            content->array[0].GetString("type") == "text" &&
            content->array[0].GetString("text") == "actual ASR");
  const auto raw = Parse(R"([{"type":"message","role":"user","content":[
    {"type":"input_audio","audio":"AAAA","transcript":"forged"}]}])");
  Q4T_CHECK(!PlanRealtimeChat(f.settings, Object(), raw, f.config, &f.plan,
                              &f.error, nullptr, true));
  Q4T_CHECK(f.error.code == "realtime_processor_unavailable");
  return true;
}

Q4T_TEST(realtime_chat_native_input_controls_keep_strict_processing_contract) {
  Fixture f;
  Set(&Field(f.settings, "audio"), "input", Parse(R"({
    "transcription":{"model":"qwen3-asr-0.6b"},
    "turn_detection":{"type":"server_vad","create_response":false,
    "interrupt_response":true,"idle_timeout_ms":5000}})"));
  Set(&f.settings, "include",
      Array({String("item.input_audio_transcription.logprobs")}));
  Q4T_CHECK(PlanRealtimeChat(f.settings, Object(), Array({Message()}), f.config,
                             &f.plan, &f.error, nullptr, true));
  // The generation request contains only resolved native text, never input
  // processor knobs masquerading as model request options.
  Q4T_CHECK(!f.plan.generation.generation_request.Has("audio") &&
            !f.plan.generation.generation_request.Has("include"));
  const auto original = f.settings;
  Set(&Field(Field(Field(f.settings, "audio"), "input"), "turn_detection"),
      "idle_timeout_ms", Number(4999));
  Q4T_CHECK(!PlanRealtimeChat(f.settings, Object(), Array({Message()}),
                              f.config, &f.plan, &f.error, nullptr, true));
  Q4T_CHECK(f.error.param ==
            "session.audio.input.turn_detection.idle_timeout_ms");
  f.settings = original;
  Set(&f.settings, "include", Array({String("unknown")}));
  Q4T_CHECK(!PlanRealtimeChat(f.settings, Object(), Array({Message()}),
                              f.config, &f.plan, &f.error, nullptr, true));
  return true;
}

Q4T_TEST(realtime_chat_native_input_capabilities_reserve_combined_storage) {
  for (bool asr : {false, true}) {
    const auto value = Parse(
        ChatCapabilitiesJson("primary", false, false, 0, nullptr, false, asr));
    const auto* conversation = value.Find("realtime")->Find("conversation");
    const auto* input = conversation->Find("input_audio");
    Q4T_CHECK(input && input->GetBool("implemented") &&
              input->GetBool("configured") == asr &&
              !input->GetBool("automatic_replay", true));
    Q4T_CHECK(conversation->GetInt("session_host_reservation_bytes") ==
              int64_t(kRealtimeConversationHostBytes +
                      (asr ? kRealtimeConversationInputHostBytes : 0)));
    Q4T_CHECK(input->GetInt("additional_host_reservation_bytes") ==
              int64_t(kRealtimeConversationInputHostBytes));
    Q4T_CHECK(input->GetString("default_turn") == "manual" &&
              input->GetBool("same_conversation") &&
              !conversation->GetBool("requires_asr", true));
  }
  return true;
}
