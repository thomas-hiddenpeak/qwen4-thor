#include "q4t/server/tool_result_contracts.h"
#include "q4t/server/tool_result_schema.h"

#include <cstdlib>
#include <filesystem>
#include <memory>

#include "../src/server/object_protocol.h"
#include "q4t/server/conversation_state.h"
#include "q4t/server/responses_execution.h"
#include "q4t/server/schema_graph.h"
#include "q4t/test.h"

namespace {
using namespace q4t::server;
using namespace q4t::server::object_detail;
using namespace std::chrono_literals;
using Clock = std::chrono::steady_clock;
Json Parse(const std::string& text) {
  Json result;
  if (!q4t::io::ParseJson(text, &result, true))
    throw std::runtime_error("fixture JSON");
  return result;
}
StateAccess Access() {
  return {"client:tool-contracts", Clock::now() + 5s, {}};
}
bool Ok(const ObjectReply& result) {
  if (!result.ok())
    std::fprintf(stderr, "%d %s %s: %s\n", result.http_status,
                 result.code.c_str(), result.value.GetString("param").c_str(),
                 result.message.c_str());
  return result.ok();
}
ObjectReply Validate(const Json& schema, const std::string& value) {
  return ValidateToolResultSchemas(
      Array({Object({{"schema", schema},
                     {"output", String(value)},
                     {"param", String("input[2].output")}})}),
      {"client:tool-contracts", Clock::now() + 5s, {}});
}
struct Fixture {
  ObjectStoreOptions options;
  Fixture() {
    const auto* root = std::getenv("Q4T_TEST_OUTPUT_DIR");
    if (!root) throw std::runtime_error("Missing tool fixture output");
    auto directory =
        (std::filesystem::path(root) / "tool-result-XXXXXX").string();
    if (!mkdtemp(directory.data()))
      throw std::runtime_error("Tool fixture directory");
    options.directory = directory + "/objects";
    options.restart_backoff_ms = 0;
  }
};
ChatPlanConfig Config() {
  ChatPlanConfig config;
  config.model = "cpu";
  config.structured = true;
  return config;
}
Json Request() {
  return Parse(R"({"input":"query","reasoning":{"effort":"none"},
    "tools":[{"type":"namespace","name":"project","description":"Records",
      "tools":[{"type":"function","name":"lookup","output_schema":{
        "type":"array","items":{"type":"integer","minimum":0},"minItems":1}}]}]})");
}
bool Emit(ResponsesExecution* run) {
  std::string wire;
  if (!Ok(run->Begin(&wire))) return false;
  ResponseDelta delta;
  delta.kind = ResponseDelta::Kind::kTool;
  delta.tool_start = true;
  delta.call = {0, "call_lookup",
                ResponsesToolModelName("project", "lookup", "function"), "{}",
                false};
  return run->Append(delta, "null", &wire) &&
         Ok(run->Finish(false, 1, {8, 0, 3, 0}, 43, &wire));
}
Json Follow(std::string value) {
  auto result = Request();
  Set(&result, "input",
      Array({Object({{"type", String("function_call_output")},
                     {"call_id", String("call_lookup")},
                     {"output", String(std::move(value))}})}));
  // A changed definition must not replace an outstanding invocation's schema.
  auto& tool = Field(Field(result, "tools").array[0], "tools").array[0];
  Set(&tool, "output_schema", Parse(R"({"type":"string"})"));
  return result;
}
}  // namespace

Q4T_TEST(tool_result_schema_json_roots_open_objects_and_exact_numbers) {
  for (const auto& [schema, value] :
       std::vector<std::pair<std::string, std::string>>{
           {"{}", "null"},
           {R"({"type":"array"})", "[1,true]"},
           {R"({"type":"object"})", R"({"extra":1})"},
           {R"({"type":"object","additionalProperties":{"type":"integer"}})",
            R"({"a":2})"},
           {R"({"type":"integer","minimum":9007199254740993})",
            "9007199254740993"},
           {R"({"anyOf":[{"type":"null"},{"type":"string"}]})", "null"},
           {R"({"type":"array","items":true})", "[1,{}]"}}) {
    Q4T_CHECK(Ok(Validate(Parse(schema), value)));
  }
  for (const auto& [schema, value] :
       std::vector<std::pair<std::string, std::string>>{
           {R"({"type":"integer","minimum":9007199254740993})",
            "9007199254740992"},
           {R"({"required":["id"]})", "{}"},
           {R"({"type":"object","additionalProperties":false})", R"({"x":1})"},
           {R"({"items":false})", "[1]"},
           {"{}", R"({"x":1,"x":2})"}}) {
    auto result = Validate(Parse(schema), value);
    Q4T_CHECK(result.http_status == 400 &&
              result.code == "invalid_tool_output" &&
              result.value.GetString("param") == "input[2].output");
  }
  return true;
}

Q4T_TEST(tool_result_schema_formats_patterns_and_definition_errors) {
  Q4T_CHECK(Ok(Validate(Parse(R"({"format":"date"})"), "\"2024-02-29\"")));
  Q4T_CHECK(
      Validate(Parse(R"({"format":"date"})"), "\"2023-02-29\"").http_status ==
      400);
  auto checks =
      Array({Object({{"schema", Parse(R"({"type":"string","pattern":"["})")},
                     {"param", String("tools[0].output_schema")}})});
  auto result =
      ValidateToolResultSchemas(checks, {"client:test", Clock::now() + 5s, {}});
#ifdef Q4T_ENABLE_XGRAMMAR
  Q4T_CHECK(result.http_status == 400 &&
            result.code == "invalid_tool_output_schema");
  Q4T_CHECK(Ok(Validate(Parse(R"({"pattern":"^rev[0-9]+$"})"), "\"rev94\"")));
  Q4T_CHECK(Validate(Parse(R"({"pattern":"^rev[0-9]+$"})"), "\"revXX\"")
                .http_status == 400);
#else
  Q4T_CHECK(result.http_status == 503 &&
            result.code == "tool_result_validation_unavailable");
#endif
  Q4T_CHECK(result.value.GetString("param") == "tools[0].output_schema");
  return true;
}

Q4T_TEST(tool_result_schema_worker_cancel_deadline_and_independent_slot) {
  auto checks =
      Array({Object({{"schema", Object()}, {"output", String("1")}})});
  Q4T_CHECK(ValidateToolResultSchemas(
                checks, {"client:test", Clock::now() + 1s, [] { return true; }})
                .http_status == 499);
  Q4T_CHECK(
      ValidateToolResultSchemas(checks, {"client:test", Clock::now() - 1s, {}})
          .http_status == 408);
  GrammarControl control;
  control.domain = IsolatedWorkerDomain::kGrammar;
  std::unique_ptr<detail::GrammarProcess> occupied;
  Q4T_CHECK(detail::GrammarProcess::Start("--tool-result-worker", control,
                                          &occupied));
  Q4T_CHECK(Ok(Validate(Object(), "true")));
  occupied.reset();
  auto counts = std::make_shared<GrammarWorkerCounters>();
  bool cancel = false;
  control.counters = counts;
  control.worker_started = [&] { cancel = true; };
  auto result = ValidateToolResultSchemas(
      checks, {"client:test", Clock::now() + 1s, [&] { return cancel; }},
      control);
  Q4T_CHECK(result.http_status == 499 && counts->started == 1 &&
            counts->active == 0 && counts->reaped == 1);
  Q4T_CHECK(Ok(Validate(Object(), "1")));
  return true;
}

Q4T_TEST(tool_result_schema_does_not_relax_generation_contract) {
  const auto schema = Parse(R"({"type":"object"})");
  OutputSchemaGraph generation, tool;
  std::string param;
  Q4T_CHECK(!generation.Init(schema, false, &param) &&
            param == "schema.additionalProperties");
  Q4T_CHECK(tool.Init(schema, false, &param, "schema",
                      OutputSchemaPurpose::kToolResult));
  Q4T_CHECK(tool.Check(Parse(R"({"open":42})")));
  return true;
}

Q4T_TEST(
    tool_result_invocation_schema_survives_restart_changed_tools_and_refs) {
  Fixture f;
  std::string response_id, item_id;
  Json call;
  {
    ObjectStore objects(f.options);
    ResponsesExecution run(objects);
    Q4T_CHECK(Ok(run.Prepare(Request(), Config(), Access(), 42)) && Emit(&run));
    response_id = run.Id();
    const auto stored = ResponsesState(objects).Retrieve(response_id, Access());
    Q4T_CHECK(Ok(stored));
    call = stored.value.GetArray("output")->array[0];
    item_id = call.GetString("id");
  }
  ObjectStore objects(f.options);
  for (bool valid : {false, true}) {
    ResponsesExecution run(objects);
    auto request = Follow(valid ? "[1]" : "\"changed\"");
    Set(&request, "previous_response_id", String(response_id));
    const auto reply = run.Prepare(request, Config(), Access(), 44);
    Q4T_CHECK(valid ? Ok(reply) : reply.code == "invalid_tool_output");
  }
  for (bool reference : {false, true}) {
    ResponsesExecution run(objects);
    auto request = Follow("\"bypass\"");
    auto& input = Field(request, "input").array;
    input.insert(input.begin(),
                 reference ? Object({{"id", String(item_id)}}) : call);
    Q4T_CHECK(run.Prepare(request, Config(), Access(), 44).code ==
              "invalid_tool_output");
  }
  return true;
}

Q4T_TEST(tool_result_contract_conversation_copy_and_frozen_queue_keep_schema) {
  Fixture f;
  ObjectStore objects(f.options);
  ResponsesExecution run(objects);
  Q4T_CHECK(Ok(run.Prepare(Request(), Config(), Access(), 42)) && Emit(&run));
  auto response = ResponsesState(objects).Retrieve(run.Id(), Access());
  auto call = response.value.GetArray("output")->array[0];
  auto conversation =
      ConversationState(objects).Create(Object(), Array({call}), Access());
  Q4T_CHECK(Ok(conversation));
  auto invalid = Follow("\"bad\"");
  Set(&invalid, "conversation", String(conversation.value.GetString("id")));
  ResponsesExecution rejected(objects);
  Q4T_CHECK(rejected.Prepare(invalid, Config(), Access(), 44).code ==
            "invalid_tool_output");
  auto request = Follow("[4]");
  Set(&request, "previous_response_id", String(run.Id()));
  Set(&request, "background", Boolean(true));
  ResponsesExecution next(objects);
  Q4T_CHECK(Ok(next.Prepare(request, Config(), Access(), 44, true)));
  Json inputs;
  Q4T_CHECK(Ok(next.FreezeInputs(&inputs)) &&
            inputs.GetArray("tool_contracts") &&
            inputs.GetArray("tool_contracts")->array.size() == 1);
  ResponsesExecution restored(objects);
  Q4T_CHECK(Ok(restored.RestoreInputs(inputs, Config(), Access(), 45)));
  return true;
}

Q4T_TEST(tool_result_schema_content_array_and_legacy_unbound_are_independent) {
  Fixture f;
  ObjectStore objects(f.options);
  ResponsesExecution run(objects);
  Q4T_CHECK(Ok(run.Prepare(Request(), Config(), Access(), 42)) && Emit(&run));
  auto request = Follow("unused");
  Set(&Field(request, "input").array[0], "output",
      Parse(R"([{"type":"input_text","text":"Not JSON"}])"));
  Set(&request, "previous_response_id", String(run.Id()));
  ResponsesExecution next(objects);
  Q4T_CHECK(Ok(next.Prepare(request, Config(), Access(), 44)));
  auto old = Request();
  auto& tool = Field(Field(old, "tools").array[0], "tools").array[0];
  std::erase_if(tool.object, [](const auto& field) {
    return field.first == "output_schema";
  });
  ResponsesExecution legacy(objects);
  Q4T_CHECK(Ok(legacy.Prepare(old, Config(), Access(), 42)) && Emit(&legacy));
  auto follow = Follow("Not JSON");
  Set(&follow, "previous_response_id", String(legacy.Id()));
  ResponsesExecution unbound(objects);
  Q4T_CHECK(Ok(unbound.Prepare(follow, Config(), Access(), 44)));
  return true;
}
