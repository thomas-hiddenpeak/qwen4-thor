#include "q4t/server/decision_plan.h"

#include <array>
#include <cmath>
#include <limits>

#include "../src/server/responses_json.h"
#include "q4t/server/output_schema.h"
#include "q4t/test.h"

namespace {
using namespace q4t::server;
using namespace q4t::server::responses_detail;

Json Question(std::string type = "predicate") {
  Json q = Object(
      {{"type", String(type)}, {"instructions", String("Classify the input")}});
  if (type == "choice")
    Set(&q, "choices",
        Array({Object({{"value", Boolean(true)}}),
               Object({{"value", String("true")}})}));
  if (type == "score")
    Set(&q, "levels",
        Array({Object({{"label", String("low")}}),
               Object({{"label", String("middle")}}),
               Object({{"label", String("high")}})}));
  return q;
}
Json Request(Json question) {
  return Object({{"model", String("local")},
                 {"input", String("project record")},
                 {"questions", Array({std::move(question)})}});
}
ChatPlanConfig Config() {
  ChatPlanConfig config;
  config.model = "local";
  config.model_aliases = {"alias"};
  return config;
}
}  // namespace

Q4T_TEST(decision_request_uses_registered_model_and_typed_choices) {
  auto request = Request(Question("choice"));
  ChatPlanError error;
  Q4T_CHECK(CheckDecisionRequest(request, Config(), &error));
  Set(&request, "model", String("alias"));
  Q4T_CHECK(CheckDecisionRequest(request, Config(), &error));
  Set(&request, "model", String("unknown"));
  Q4T_CHECK(!CheckDecisionRequest(request, Config(), &error) &&
            error.http_status == 404);
  Set(&request, "model", String("local"));
  auto& choices = Field(Field(request, "questions").array[0], "choices");
  choices.array[1] = choices.array[0];
  Q4T_CHECK(!CheckDecisionRequest(request, Config(), &error) &&
            error.param == "questions[0].choices");
  Set(&request, "stream", Boolean(true));
  Q4T_CHECK(!CheckDecisionRequest(request, Config(), &error));
  return true;
}
Q4T_TEST(decision_request_enforces_question_and_option_cardinality) {
  ChatPlanError error;
  auto request = Request(Question());
  auto& questions = Field(request, "questions").array;
  questions.assign(400, Question());
  Q4T_CHECK(CheckDecisionRequest(request, Config(), &error));
  questions.push_back(Question());
  Q4T_CHECK(!CheckDecisionRequest(request, Config(), &error));
  questions.clear();
  Q4T_CHECK(!CheckDecisionRequest(request, Config(), &error));
  questions.push_back(Question("score"));
  auto& levels = Field(questions[0], "levels").array;
  auto first = levels.front();
  levels.assign(11, first);
  Q4T_CHECK(!CheckDecisionRequest(request, Config(), &error));
  levels.resize(1);
  Q4T_CHECK(!CheckDecisionRequest(request, Config(), &error));
  return true;
}
Q4T_TEST(decision_history_checks_tool_lineage_and_cache_breakpoints) {
  ChatPlanError error;
  auto request = Request(Question());
  auto call = Object({{"type", String("custom_tool_call")},
                      {"call_id", String("one")},
                      {"name", String("read_project")},
                      {"input", String("state")}});
  auto result = Object({{"type", String("custom_tool_call_output")},
                        {"call_id", String("one")},
                        {"output", String("rev426")}});
  Set(&request, "input", Array({result, call}));
  Q4T_CHECK(!CheckDecisionRequest(request, Config(), &error));
  Set(&request, "input", Array({call, result}));
  Q4T_CHECK(CheckDecisionRequest(request, Config(), &error));
  Set(&result, "type", String("function_call_output"));
  Set(&request, "input", Array({call, result}));
  Q4T_CHECK(!CheckDecisionRequest(request, Config(), &error));
  auto part = Object(
      {{"type", String("input_text")},
       {"text", String("frozen")},
       {"prompt_cache_breakpoint", Object({{"mode", String("explicit")}})}});
  auto message = Object(
      {{"role", String("user")}, {"content", Array({part, part, part, part})}});
  Set(&request, "input", Array({message}));
  Q4T_CHECK(CheckDecisionRequest(request, Config(), &error));
  Field(Field(request, "input").array.front(), "content").array.push_back(part);
  Q4T_CHECK(!CheckDecisionRequest(request, Config(), &error) &&
            error.param == "input[0].content[4].prompt_cache_breakpoint");
  return true;
}
Q4T_TEST(decision_requests_keep_json_payload_separate_from_protocol_fields) {
  ChatPlanError error;
  auto request = Request(Question());
  auto data = Object({{"type", String("input_image")},
                      {"image_url", String("local-file")},
                      {"prompt_cache_breakpoint", Boolean(true)}});
  auto part = Object({{"type", String("input_json")}, {"data", data}});
  auto& question = Field(request, "questions").array.front();
  Set(&question, "instructions", Array({part}));
  Q4T_CHECK(CheckDecisionRequest(request, Config(), &error));
  Set(&Field(question, "instructions").array.front(), "data",
      Object({{"large", String(std::string(256ULL << 10, 'x'))}}));
  Q4T_CHECK(!CheckDecisionRequest(request, Config(), &error) &&
            error.param == "questions[0].instructions[0].data");
  Set(&question, "instructions",
      Array({Object({{"type", String("input_image")},
                     {"image_url", String("file:///tmp/test")}})}));
  Q4T_CHECK(!CheckDecisionRequest(request, Config(), &error));
  return true;
}
Q4T_TEST(decision_projection_preserves_probability_mass_refusal_and_order) {
  Json answer;
  auto choice = Question("choice");
  std::array<double, 2> weights = {-1002., -1000.};
  Q4T_CHECK(ProjectDecisionAnswer(choice, weights, .6, false, &answer));
  Q4T_CHECK(answer.Find("choice")->IsString() && answer.Find("name")->IsNull());
  Q4T_CHECK(answer.Find("probabilities")->array[0].Find("value")->IsBool());
  Q4T_CHECK(std::abs(answer.GetNumber("confidence") - .6) < 1e-12);
  auto score = Question("score");
  Set(&score, "name", String("priority"));
  Q4T_CHECK(ProjectDecisionAnswer(
      score, std::array{std::log(.1), std::log(.7), std::log(.2)}, .55, false,
      &answer));
  Q4T_CHECK(std::abs(answer.GetNumber("score") - 1.1) < 1e-12 &&
            answer.GetString("name") == "priority" &&
            answer.Find("probabilities")->array[1].GetString("label") ==
                "middle");
  Q4T_CHECK(ProjectDecisionAnswer(score, {}, 0, true, &answer));
  Q4T_CHECK(answer.GetString("type") == "refusal" && !answer.Has("score"));
  auto before = OutputJson(answer);
  Q4T_CHECK(!ProjectDecisionAnswer(
      Question(), std::array{0., std::numeric_limits<double>::quiet_NaN()}, .5,
      false, &answer));
  Q4T_CHECK(!ProjectDecisionAnswer(Question(), {}, .5, false, &answer) &&
            OutputJson(answer) == before);
  Q4T_CHECK(ProjectDecisionAnswer(
      Question(), std::array{-std::numeric_limits<double>::infinity(), 0.}, .5,
      false, &answer));
  Q4T_CHECK(answer.GetNumber("probability") == 1);
  return true;
}

Q4T_TEST(decision_result_requires_usage_and_keeps_refusals_per_question) {
  Json predicate, refusal;
  Q4T_CHECK(ProjectDecisionAnswer(Question(), std::array{0., 0.}, 0., false,
                                  &predicate));
  Q4T_CHECK(ProjectDecisionAnswer(Question("score"), {}, 0., true, &refusal));
  auto usage = Object(
      {{"input_tokens", Number(4)},
       {"output_tokens", Number(2)},
       {"total_tokens", Number(6)},
       {"input_tokens_details", Object({{"cached_tokens", Number(0)},
                                        {"cache_write_tokens", Number(0)}})},
       {"output_tokens_details", Object({{"reasoning_tokens", Number(0)}})}});
  auto result = Object(
      {{"model", String("local")}, {"answers", Array({predicate, refusal})}});
  ChatPlanError error;
  Q4T_CHECK(!ValidateDecisionShape(result, DecisionShape::kResult, &error));
  Set(&result, "usage", usage);
  Q4T_CHECK(ValidateDecisionShape(result, DecisionShape::kResult, &error));
  Set(&Field(result, "answers").array[1], "probability", Number(1));
  Q4T_CHECK(!ValidateDecisionShape(result, DecisionShape::kResult, &error));
  return true;
}

Q4T_TEST(decision_result_keeps_actual_token_subtotals_and_question_order) {
  auto a = Question(), b = Question("choice");
  Set(&b, "name", String("route"));
  auto request = Request(a);
  Field(request, "questions").array.push_back(b);
  Json first, second, result;
  Q4T_CHECK(ProjectDecisionAnswer(a, std::array{0., .9}, .5, false, &first));
  Q4T_CHECK(ProjectDecisionAnswer(b, {}, 0., true, &second));
  ActualUsage usage{35, 2, 8, 1, 3};
  Q4T_CHECK(
      BuildDecisionResult(request, Array({first, second}), usage, &result));
  Q4T_CHECK(result.Find("usage")->GetInt("total_tokens") == 37 &&
            result.Find("usage")
                    ->Find("input_tokens_details")
                    ->GetInt("cache_write_tokens") == 3);
  const auto before = OutputJson(result);
  Q4T_CHECK(
      !BuildDecisionResult(request, Array({second, first}), usage, &result));
  Q4T_CHECK(!BuildDecisionResult(request, Array({first}), usage, &result));
  usage.cached = 34;
  Q4T_CHECK(
      !BuildDecisionResult(request, Array({first, second}), usage, &result));
  Q4T_CHECK(OutputJson(result) == before);
  return true;
}
