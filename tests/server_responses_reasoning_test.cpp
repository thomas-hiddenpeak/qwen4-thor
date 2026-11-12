#include "q4t/server/responses_reasoning.h"

#include <array>

#include "q4t/server/chat_prompt.h"
#include "q4t/server/output_schema.h"
#include "q4t/test.h"

namespace {
using namespace q4t::server;
using q4t::io::Json;
Json Read(const std::string& text) {
  Json value;
  if (!q4t::io::ParseJson(text, &value, true))
    throw std::runtime_error("pro fixture JSON");
  return value;
}
ChatPlanConfig Config() {
  ChatPlanConfig config;
  config.model = "cpu";
  config.structured = true;
  config.max_len = 8192;
  config.default_max_tokens = 2048;
  return config;
}
bool Source(const std::string& raw, ResponsesGenerationPlan* generation,
            ResponsesEnvelope* envelope = nullptr) {
  ResponsesEnvelope parsed;
  ChatPlanError error;
  auto status = ParseResponsesEnvelope(Read(raw), Config(), &parsed, &error);
  if (status)
    status = PrepareResponsesGeneration(parsed, *parsed.request.Find("input"),
                                        Config(), generation, &error);
  if (!status) std::fprintf(stderr, "%s\n", status.message().c_str());
  if (envelope) *envelope = std::move(parsed);
  return status.ok();
}
std::string Prompt(const ChatRequestPlan& plan, const Json& original) {
  std::string result;
  std::string error_param;
  std::vector<VisionItem> media;
  const auto status = PrepareChatPrompt(plan.Request(original),
                                        plan.constraints, &result, &media,
                                        &error_param);
  if (!status) throw std::runtime_error(status.message());
  return result;
}
}  // namespace

Q4T_TEST(responses_pro_effort_independent_and_unknown_mode_rejected) {
  for (const auto* effort : {"none", "low", "medium", "xhigh"}) {
    ResponsesGenerationPlan source;
    ResponsesEnvelope envelope;
    Q4T_CHECK(Source(std::string(R"({"input":"17+25?","reasoning":{)"
                                 R"("mode":"pro","effort":")") +
                         effort + R"("},"max_output_tokens":384})",
                     &source, &envelope));
    ResponsesSummaryPlan work;
    ChatPlanError error;
    auto config = Config();
    config.structured = false;
    Q4T_CHECK(
        PlanResponsesDeliberation(source, 384, 0, config, &work, &error));
    Q4T_CHECK(envelope.reasoning_mode == "pro" &&
              envelope.reasoning_effort == effort && work.needed);
    Q4T_CHECK(work.request.GetBool("enable_thinking") ==
              source.generation_request.GetBool("enable_thinking"));
    Q4T_CHECK(work.request.GetInt("max_completion_tokens") == 128);
    Q4T_CHECK(!work.chat.constraints.active);
  }
  ResponsesEnvelope envelope;
  ChatPlanError error;
  Q4T_CHECK(!ParseResponsesEnvelope(
      Read(R"({"input":"x","reasoning":{"mode":"fake"}})"), Config(), &envelope,
      &error));
  Q4T_CHECK(error.http_status == 400 && error.param == "reasoning.mode");
  return true;
}

Q4T_TEST(responses_pro_work_preserves_context_but_cannot_authorize_tools) {
  ResponsesGenerationPlan source;
  Q4T_CHECK(Source(R"({"input":"Read docs/MODEL.md first.",
    "instructions":"Use actual tool evidence.","reasoning":{"mode":"pro","effort":"none"},
    "tools":[{"type":"function","name":"read","parameters":{"type":"object","properties":{},"required":[],"additionalProperties":false},"strict":true}],
    "tool_choice":"required","max_output_tokens":384})",
                   &source));
  const auto original = OutputJson(source.generation_request);
  ResponsesSummaryPlan work;
  ChatPlanError error;
  Q4T_CHECK(PlanResponsesDeliberation(source, 384, 0, Config(), &work, &error));
  Q4T_CHECK(work.request.GetString("tool_choice") == "none" &&
            work.chat.constraints.choice ==
                OutputConstraints::ToolChoice::kNone);
  Q4T_CHECK(OutputJson(*work.request.Find("tools")) ==
            OutputJson(*source.generation_request.Find("tools")));
  const auto prompt = Prompt(work.chat, work.request);
  Q4T_CHECK(prompt.find("Read docs/MODEL.md first.") != prompt.npos &&
            prompt.find("Do not call tools") != prompt.npos);
  ResponsesGenerationPlan answer;
  Q4T_CHECK(PlanResponsesDeliberatedAnswer(source, "Need actual read result.",
                                           384, 80, Config(), &answer, &error));
  Q4T_CHECK(answer.generation_request.GetString("tool_choice") == "required" &&
            answer.chat.constraints.choice ==
                OutputConstraints::ToolChoice::kRequired);
  Q4T_CHECK(OutputJson(source.generation_request) == original);
  return true;
}

Q4T_TEST(responses_pro_final_preserves_schema_and_uses_actual_notes_as_data) {
  ResponsesGenerationPlan source;
  Q4T_CHECK(Source(
      R"({"input":"What is 17+25?","reasoning":{"mode":"pro","effort":"none"},
    "text":{"format":{"type":"json_schema","name":"answer","strict":true,
      "schema":{"type":"object","properties":{"answer":{"type":"integer"}},"required":["answer"],"additionalProperties":false}}}})",
      &source));
  const auto original = OutputJson(source.generation_request);
  ResponsesSummaryPlan work;
  ChatPlanError error;
  Q4T_CHECK(
      PlanResponsesDeliberation(source, 256, 20, Config(), &work, &error));
  Q4T_CHECK(!work.chat.constraints.json_output);
  ResponsesGenerationPlan answer;
  Q4T_CHECK(PlanResponsesDeliberatedAnswer(source, "17 + 25 = 42; checked.",
                                           256, 84, Config(), &answer, &error));
  Q4T_CHECK(answer.chat.constraints.json_output &&
            answer.generation_request.GetInt("max_completion_tokens") == 172);
  const auto* messages = answer.generation_request.GetArray("messages");
  Q4T_CHECK(messages && messages->array.size() >= 3);
  const auto& notes = messages->array[messages->array.size() - 2];
  Q4T_CHECK(notes.GetString("role") == "assistant" &&
            notes.GetString("content").find("17 + 25 = 42; checked.") !=
                std::string::npos);
  Q4T_CHECK(Prompt(answer.chat, answer.generation_request)
                .find("17 + 25 = 42; checked.") != std::string::npos);
  Q4T_CHECK(OutputJson(*answer.generation_request.Find("response_format")) ==
            OutputJson(*source.generation_request.Find("response_format")));
  Q4T_CHECK(OutputJson(source.generation_request) == original);
  return true;
}

Q4T_TEST(responses_pro_budget_limits_after_compaction_and_tiny_request) {
  ResponsesGenerationPlan source;
  Q4T_CHECK(Source(R"({"input":"x","reasoning":{"mode":"pro"}})", &source));
  ResponsesSummaryPlan work;
  ChatPlanError error;
  for (const auto [limit, used, expected] :
       {std::array<int, 3>{2048, 0, 512}, {384, 96, 96}, {1, 0, 1}}) {
    Q4T_CHECK(PlanResponsesDeliberation(source, limit, used, Config(), &work,
                                        &error));
    Q4T_CHECK(work.needed && !work.budget_exhausted &&
              work.request.GetInt("max_completion_tokens") == expected);
  }
  Q4T_CHECK(
      PlanResponsesDeliberation(source, 256, 256, Config(), &work, &error));
  Q4T_CHECK(!work.needed && work.budget_exhausted);
  for (const auto [limit, used] :
       {std::array<int, 2>{0, 0}, {256, -1}, {256, 257}}) {
    Q4T_CHECK(!PlanResponsesDeliberation(source, limit, used, Config(), &work,
                                         &error));
    Q4T_CHECK(error.http_status == 500 && !work.needed);
  }
  ResponsesGenerationPlan answer;
  Q4T_CHECK(!PlanResponsesDeliberatedAnswer(source, "notes", 1, 1, Config(),
                                            &answer, &error));
  Q4T_CHECK(error.code == "response_deliberation_incomplete");
  return true;
}

Q4T_TEST(responses_pro_notes_bounds_and_no_fabricated_continuation) {
  ResponsesGenerationPlan source, answer;
  Q4T_CHECK(Source(R"({"input":"x","reasoning":{"mode":"pro"}})", &source));
  ChatPlanError error;
  for (const auto& notes : {std::string(), std::string(1024 * 1024 + 1, 'x')}) {
    Q4T_CHECK(!PlanResponsesDeliberatedAnswer(source, notes, 256, 20, Config(),
                                              &answer, &error));
    Q4T_CHECK(error.code == "response_deliberation_incomplete" &&
              answer.generation_request.IsNull());
  }
  ResponsesGenerationPlan absent;
  ResponsesSummaryPlan work;
  Q4T_CHECK(
      !PlanResponsesDeliberation(absent, 256, 0, Config(), &work, &error));
  Q4T_CHECK(error.code == "response_state_invalid");
  return true;
}

Q4T_TEST(responses_pro_original_history_and_cache_boundaries_do_not_mutate) {
  ResponsesGenerationPlan source;
  Q4T_CHECK(Source(R"({"input":[{"role":"user","content":[
    {"type":"input_text","text":"actual prefix","prompt_cache_breakpoint":{"mode":"explicit"}},
    {"type":"input_text","text":" current task"}]}],
    "reasoning":{"mode":"pro","effort":"none"},"prompt_cache_options":{"mode":"explicit"}})",
                   &source));
  const auto history = OutputJson(source.input.items);
  Q4T_CHECK(source.input.cache_boundaries.size() == 1);
  ResponsesGenerationPlan answer;
  ChatPlanError error;
  Q4T_CHECK(PlanResponsesDeliberatedAnswer(source, "actual generated notes",
                                           256, 50, Config(), &answer, &error));
  Q4T_CHECK(OutputJson(answer.input.items) == history &&
            OutputJson(source.input.items) == history);
  Q4T_CHECK(answer.input.cache_boundaries.size() == 1 &&
            answer.input.cache_boundaries[0].message_index ==
                source.input.cache_boundaries[0].message_index);
  Q4T_CHECK(answer.generation_request.GetArray("messages")->array.size() ==
            source.generation_request.GetArray("messages")->array.size() + 2);
  return true;
}
