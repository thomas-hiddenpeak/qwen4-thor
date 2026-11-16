#include "q4t/server/chat_moderation.h"

#include <cstdlib>
#include <filesystem>
#include <fstream>

#include "q4t/server/chat_contract.h"
#include "q4t/server/chat_choices.h"
#include "q4t/server/output_schema.h"
#include "q4t/test.h"

namespace {
using namespace q4t::server;
using q4t::io::Json;
Json Read(const std::string& text) {
  Json value;
  if (!q4t::io::ParseJson(text, &value, true))
    throw std::runtime_error("Chat moderation fixture JSON");
  return value;
}
ChatPlanConfig Config() {
  ChatPlanConfig value;
  value.model = "local-model";
  value.model_aliases = {"local-alias"};
  value.structured = true;
  return value;
}
ModerationOptions Options(bool block = true) {
  return {true, "local-model",
          block ? ModerationMode::kBlock : ModerationMode::kScore,
          block ? ModerationMode::kBlock : ModerationMode::kScore};
}
Json Report(bool flagged = false) {
  ModerationPlan plan;
  plan.model = "local-model";
  plan.text = true;
  Json result;
  const auto raw = flagged ? R"({"scores":[0,0,0,0,0,0,0,0,0,0,0,0.9,0]})"
                           : R"({"scores":[0,0,0,0,0,0,0,0,0,0,0,0.1,0]})";
  if (!ParseModerationResult(raw, plan, 0.5, &result))
    throw std::runtime_error("Chat classification report");
  return result;
}
std::string Candidate() {
  ChatChoices value(2, false);
  if (!value.Add(
          0,
          R"({"role":"assistant","content":"SECRET_TEXT","reasoning_content":"SECRET_REASON"})",
          "stop", "null", 20, 4, 0, false, -1, 0, 0) ||
      !value.Add(
          1,
          R"({"role":"assistant","content":null,"tool_calls":[{"type":"function","id":"call_read","function":{"name":"read","arguments":"SECRET_ARGUMENT"}}]})",
          "tool_calls", R"({"content":[{"token":"SECRET_PROBABILITY"}]})", 20,
          5, 0, false, -1, 0, 0))
    throw std::runtime_error("Chat moderation candidate");
  std::string result;
  if (!value.Json("chatcmpl_review", "local-model", 1, &result))
    throw std::runtime_error("Chat moderation response");
  return result;
}
void Artifact(const char* name, const std::string& data) {
  const auto* output = std::getenv("Q4T_TEST_OUTPUT_DIR");
  if (!output) return;
  std::ofstream(std::filesystem::path(output) / name) << data;
}
}  // namespace

Q4T_TEST(chat_moderation_plan_null_alias_and_independent_policy) {
  ChatRequestPlan plan;
  ChatPlanError error;
  for (
      const auto* value :
      {"null",
       R"({"model":"local-alias","policy":{"input":{"mode":"block"},"output":null}})"}) {
    const auto request = Read(
        std::string(
            R"({"messages":[{"role":"user","content":"hello"}],"moderation":)") +
        value + '}');
    Q4T_CHECK(PlanChatRequest(request, Config(), &plan, &error));
    if (plan.moderation.enabled)
      Q4T_CHECK(plan.moderation.model == "local-model" &&
                plan.moderation.input == ModerationMode::kBlock &&
                plan.moderation.output == ModerationMode::kScore);
  }
  return true;
}
Q4T_TEST(chat_moderation_contract_nested_errors_and_actual_backend) {
  for (
      const auto* bad :
      {R"({})", R"({"model":"local-model","policy":{"input":{"mode":"skip"}}})",
       R"({"model":"local-model","policy":{"output":{"mode":"block","bypass":true}}})"}) {
    const auto request = Read(
        std::string(
            R"({"messages":[{"role":"user","content":"hello"}],"moderation":)") +
        bad + '}');
    std::string param;
    Q4T_CHECK(!ValidateChatContract(request, "local-model", false, &param));
    Q4T_CHECK(param.starts_with("moderation"));
  }
  ChatRequestPlan plan;
  ChatPlanError error;
  Q4T_CHECK(!PlanChatRequest(
      Read(R"({"prompt":"hello","moderation":{"model":"unserved"}})"), Config(),
      &plan, &error));
  Q4T_CHECK(error.http_status == 404 && error.param == "moderation.model");
  return true;
}
Q4T_TEST(chat_moderation_plan_keeps_multiple_choices_tools_and_raw_modes) {
  for (const auto* mode : {"true", "false"}) {
    ChatRequestPlan plan;
    ChatPlanError error;
    const auto request = Read(
        std::string(
            R"({"messages":[{"role":"user","content":"hello"}],"n":2,"parse_response":)") +
        mode + R"(,"moderation":{"model":"local-model"}})");
    Q4T_CHECK(PlanChatRequest(request, Config(), &plan, &error));
    Q4T_CHECK(plan.sampling.choices == 2 && plan.moderation.enabled);
  }
  return true;
}
Q4T_TEST(chat_moderation_block_input_never_requires_or_exposes_draft) {
  ChatModeration state(Options());
  Q4T_CHECK(state.Record(true, Report(true), {12, 9, 1}));
  std::string empty, result;
  Q4T_CHECK(ChatModeration::EmptyCompletion(2, "chatcmpl_review", "local-model",
                                            1, &empty));
  Q4T_CHECK(state.Project(empty, &result));
  const auto value = Read(result);
  Q4T_CHECK(value.GetArray("choices")->array.size() == 2);
  Q4T_CHECK(value.Find("usage")->GetInt("total_tokens") == 0);
  Q4T_CHECK(value.Find("moderation")->Find("output")->GetString("type") ==
            "error");
  Q4T_CHECK(!state.Release(&empty));
  Artifact("chat-moderation-input-block.json", result);
  return true;
}
Q4T_TEST(chat_moderation_block_output_discards_every_choice_and_probability) {
  ChatModeration state(Options());
  Q4T_CHECK(state.Record(true, Report(), {10, 8, 1}));
  Q4T_CHECK(state.Capture("data: SECRET_STREAM\r\n\r\n"));
  Q4T_CHECK(state.Record(false, Report(true), {30, 7, 1}));
  std::string result, wire;
  Q4T_CHECK(state.Project(Candidate(), &result));
  Q4T_CHECK(result.find("SECRET_") == std::string::npos);
  Q4T_CHECK(!state.Release(&wire) && wire.empty());
  const auto value = Read(result);
  for (const auto& choice : value.GetArray("choices")->array) {
    Q4T_CHECK(choice.GetString("finish_reason") == "content_filter");
    Q4T_CHECK(choice.Find("message")->Find("content")->IsNull());
    Q4T_CHECK(choice.Find("logprobs")->IsNull());
  }
  Q4T_CHECK(value.Find("usage")->GetInt("completion_tokens") == 9);
  Q4T_CHECK(value.Find("q4t_moderation_usage")->GetInt("output_tokens") == 15);
  Artifact("chat-moderation-output-block.json", result);
  return true;
}
Q4T_TEST(chat_moderation_approved_stream_releases_original_order_once) {
  ChatModeration state(Options());
  Q4T_CHECK(state.Record(true, Report(), {10, 8, 1}));
  const std::string pieces = "role0;content0;finish0;role1;tool1;";
  Q4T_CHECK(state.Capture(pieces.substr(0, 14)));
  Q4T_CHECK(state.Capture(pieces.substr(14)));
  std::string wire;
  Q4T_CHECK(state.Record(false, Report(), {20, 9, 1}));
  Q4T_CHECK(state.Release(&wire) && wire == pieces);
  Q4T_CHECK(!state.Release(&wire) && wire.empty());
  return true;
}
Q4T_TEST(
    chat_moderation_unavailable_block_never_publishes_or_fills_safe_scores) {
  ChatModeration state(Options());
  Q4T_CHECK(state.Record(true, Report(), {10, 8, 1}));
  Q4T_CHECK(state.Capture("secret"));
  Q4T_CHECK(state.Record(false, ModerationError("timeout", "not completed"),
                         {5, 2, 1}));
  std::string output;
  Q4T_CHECK(!state.Project(Candidate(), &output) && output.empty());
  Q4T_CHECK(!state.Release(&output) && output.empty());
  Q4T_CHECK(state.StreamMetadata("id", "model", 1, true).empty());
  return true;
}
Q4T_TEST(chat_moderation_score_errors_remain_explicit_and_keep_healthy_output) {
  ChatModeration state(Options(false));
  Q4T_CHECK(state.Record(true, ModerationError("busy", "no classifier"), {}));
  Q4T_CHECK(state.Record(false, ModerationError("incomplete", "truncated"),
                         {4, 512, 1}));
  std::string output;
  Q4T_CHECK(state.Project(Candidate(), &output));
  Q4T_CHECK(output.find("SECRET_TEXT") != std::string::npos);
  Q4T_CHECK(output.find("moderation_results") == std::string::npos);
  Q4T_CHECK(
      Read(output).Find("q4t_moderation_usage")->GetInt("output_tokens") ==
      512);
  return true;
}
Q4T_TEST(chat_moderation_score_flagged_output_preserves_original_tool_finish) {
  ChatModeration state(Options(false));
  Q4T_CHECK(state.Record(true, Report(true), {10, 8, 1}));
  Q4T_CHECK(state.Record(false, Report(true), {20, 9, 1}));
  std::string output;
  Q4T_CHECK(state.Project(Candidate(), &output));
  Q4T_CHECK(Read(output).GetArray("choices")->array[1].GetString(
                "finish_reason") == "tool_calls");
  Q4T_CHECK(Read(output)
                .Find("moderation")
                ->Find("output")
                ->GetArray("results")
                ->array[0]
                .GetBool("flagged"));
  return true;
}
Q4T_TEST(
    chat_moderation_hold_capacity_is_sticky_failure_not_silent_truncation) {
  ChatModeration state(Options());
  Q4T_CHECK(state.Record(true, Report(), {10, 8, 1}));
  Q4T_CHECK(state.Capture(std::string(ChatModeration::kWireLimit, 'x')));
  Q4T_CHECK(!state.Capture("x"));
  Q4T_CHECK(state.Record(false, Report(), {20, 9, 1}));
  std::string output;
  Q4T_CHECK(!state.Release(&output));
  Q4T_CHECK(!state.Project(Candidate(), &output));
  return true;
}
Q4T_TEST(
    chat_moderation_receipts_reject_wrong_model_replay_and_invented_usage) {
  ChatModeration state(Options());
  Q4T_CHECK(!state.Record(false, Report(), {10, 8, 1}));
  Q4T_CHECK(!state.Record(true, Report(), {}));
  Q4T_CHECK(!state.Record(true, Report(), {-1, 8, 1}));
  Q4T_CHECK(!state.Record(true, Report(), {1, 513, 1}));
  auto foreign = Report();
  for (auto& [key, value] : foreign.object)
    if (key == "model") value.str = "foreign";
  Q4T_CHECK(!state.Record(true, foreign, {10, 8, 1}));
  Q4T_CHECK(state.Record(true, Report(), {10, 8, 1}));
  Q4T_CHECK(!state.Record(true, Report(), {10, 8, 1}));
  return true;
}
Q4T_TEST(chat_moderation_output_collects_all_choices_and_legacy_arguments) {
  ModerationContent content;
  ChatPlanError error;
  Q4T_CHECK(ChatModeration::CollectOutput(Candidate(), &content, &error));
  const auto actual = OutputJson(content.records);
  for (const auto* text : {"SECRET_TEXT", "SECRET_REASON", "SECRET_ARGUMENT"})
    Q4T_CHECK(actual.find(text) != std::string::npos);
  Q4T_CHECK(actual.find("SECRET_PROBABILITY") == std::string::npos);
  Q4T_CHECK(CollectModerationInput(
      Read(
          R"([{"role":"assistant","content":null,"function_call":{"name":"EXCLUDED","arguments":"LEGACY_ARGUMENT"},"refusal":"ACTUAL_REFUSAL"}])"),
      &content, &error));
  const auto legacy = OutputJson(content.records);
  Q4T_CHECK(legacy.find("LEGACY_ARGUMENT") != std::string::npos &&
            legacy.find("ACTUAL_REFUSAL") != std::string::npos &&
            legacy.find("EXCLUDED") == std::string::npos);
  return true;
}
Q4T_TEST(chat_moderation_standard_metadata_and_separate_actual_usage) {
  ChatModeration state(Options());
  Q4T_CHECK(state.Record(true, Report(), {10, 8, 1}));
  Q4T_CHECK(state.Record(false, Report(), {20, 9, 1}));
  std::string original;
  Q4T_CHECK(ChatModeration::EmptyCompletion(1, "chatcmpl_review", "local-model",
                                            1, &original));
  std::string output;
  Q4T_CHECK(state.Project(original, &output));
  const auto value = Read(output);
  const auto& reports = *value.Find("moderation");
  Q4T_CHECK(reports.Find("input")->GetString("type") == "moderation_results");
  Q4T_CHECK(reports.Find("output")->GetString("model") == "local-model");
  Q4T_CHECK(value.Find("q4t_moderation_usage")->GetInt("input_tokens") == 30);
  const auto wire =
      state.StreamMetadata("chatcmpl_review", "local-model", 1, true);
  Q4T_CHECK(wire.find("\"choices\":[]") != std::string::npos &&
            wire.find("\"usage\":null") != std::string::npos);
  Artifact("chat-moderation-approved.json", output);
  Artifact("chat-moderation-metadata.sse", wire);
  return true;
}
