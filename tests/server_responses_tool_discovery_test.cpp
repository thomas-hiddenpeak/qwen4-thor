#include "q4t/server/responses_execution.h"

#include <algorithm>
#include <cstdlib>
#include <filesystem>
#include <fstream>

#include "../src/server/object_protocol.h"
#include "q4t/server/chat_prompt.h"
#include "q4t/server/chat_capabilities.h"
#include "q4t/server/conversation_state.h"
#include "q4t/server/responses_item_shape.h"
#include "q4t/server/responses_reasoning.h"
#include "q4t/server/responses_tokens.h"
#include "q4t/server/schema_graph.h"
#include "q4t/server/schema_receipt.h"
#include "q4t/server/tool_result_contracts.h"
#include "q4t/test.h"

namespace {
using namespace q4t::server;
using namespace q4t::server::object_detail;
using namespace std::chrono_literals;
using Clock = std::chrono::steady_clock;
Json Read(const std::string& text) {
  Json value;
  const auto status = q4t::io::ParseJson(text, &value, true);
  if (!status) throw std::runtime_error(status.message());
  return value;
}
bool Ok(const ObjectReply& result) {
  if (!result.ok())
    std::fprintf(stderr, "%d %s %s: %s\n", result.http_status,
                 result.code.c_str(), result.value.GetString("param").c_str(),
                 result.message.c_str());
  return result.ok();
}
bool Ok(const q4t::Status& result, const ChatPlanError& error) {
  if (!result)
    std::fprintf(stderr, "%s %s: %s\n", error.code.c_str(), error.param.c_str(),
                 result.message().c_str());
  return result.ok();
}
struct Fixture {
  ObjectStoreOptions options;
  Fixture() {
    const auto* root = std::getenv("Q4T_TEST_OUTPUT_DIR");
    if (!root) throw std::runtime_error("Missing discovery test output");
    auto directory =
        (std::filesystem::path(root) / "discovery-XXXXXX").string();
    if (!mkdtemp(directory.data()))
      throw std::runtime_error("Discovery fixture");
    options.directory = directory + "/objects";
    options.restart_backoff_ms = 0;
  }
};
StateAccess Access() { return {"client:discovery", Clock::now() + 10s, {}}; }
ChatPlanConfig Config() {
  ChatPlanConfig result;
  result.model = "cpu";
  result.structured = true;
  return result;
}
Json Tools() {
  return Read(R"([
    {"type":"tool_search"},
    {"type":"namespace","name":"project","description":"Project docs",
     "tools":[{"type":"function","name":"read","defer_loading":true,
       "description":"private_loaded_description",
       "parameters":{"type":"object","properties":{"path":{"type":"string"}},
        "required":["path"],"additionalProperties":false},"strict":true,
       "output_schema":{"type":"object","properties":{"title":{"type":"string"}},
        "required":["title"],"additionalProperties":false}}]}])");
}
Json Request() {
  auto request = Read(R"({"input":"Find the project model documentation",
    "reasoning":{"effort":"none"},"max_output_tokens":128})");
  Set(&request, "tools", Tools());
  return request;
}
Json SearchCall(bool server = true) {
  return Object({{"id", String("ts_history")},
                 {"type", String("tool_search_call")},
                 {"execution", String(server ? "server" : "client")},
                 {"call_id", server ? Json() : String("discover")},
                 {"arguments", Read(R"({"paths":["project"]})")},
                 {"status", String("completed")}});
}
Json SearchResult(bool server = true) {
  return Object({{"id", String("tso_history")},
                 {"type", String("tool_search_output")},
                 {"execution", String(server ? "server" : "client")},
                 {"call_id", server ? Json() : String("discover")},
                 {"tools", Array({Tools().array[1]})},
                 {"status", String("completed")}});
}
ResponseDelta SearchDelta(size_t index = 0,
                          std::string args = R"({"paths":["project"]})") {
  ResponseDelta delta;
  delta.kind = ResponseDelta::Kind::kTool;
  delta.tool_start = true;
  delta.call = {
      index, "discover" + std::to_string(index),
      std::string(kResponsesToolSearchModelName),
      OutputJson(Object({{"arguments_json", String(std::move(args))}})), false};
  return delta;
}
q4t::Status Plan(const Json& request, ResponsesGenerationPlan* plan,
                 ChatPlanError* error) {
  ResponsesEnvelope envelope;
  auto status = ParseResponsesEnvelope(request, Config(), &envelope, error);
  if (status)
    status = PrepareResponsesGeneration(envelope, *request.Find("input"),
                                        Config(), plan, error);
  return status;
}
q4t::Status Prompt(const ResponsesGenerationPlan& plan, std::string* prompt,
                   ChatPlanError* error) {
  std::vector<VisionItem> media;
  return PrepareChatPrompt(
      plan.chat.Request(plan.generation_request), plan.chat.constraints, prompt,
      &media, &error->param, &error->code, nullptr, false,
      plan.chat.async_tool_call_ids, plan.chat.responses_tool_media,
      plan.chat.HeaderTools(), plan.chat.tool_call_formats);
}
bool Silent(const std::string&) { return true; }
}  // namespace

Q4T_TEST(responses_discovery_client_open_boolean_and_scalar_schemas_execute) {
  const std::vector<std::pair<std::string, std::string>> cases = {
      {"null", R"({"anything":[1,true]})"},
      {"true", R"(["any",null])"},
      {"false", "{}"},
      {R"({"type":"string","const":"ready"})", "\"ready\""},
      {R"({"type":"object","properties":{"x":{"type":"integer"}}})",
       R"({"x":1,"extra":"allowed"})"}};
  for (const auto& [schema, arguments] : cases) {
    Fixture fixture;
    ObjectStore objects(fixture.options);
    auto request = Request();
    Set(&request, "tools",
        Array({Object({{"type", String("tool_search")},
                       {"execution", String("client")},
                       {"parameters", Read(schema)}})}));
    ResponsesExecution run(objects);
    Q4T_CHECK(Ok(run.Prepare(request, Config(), Access(), 42)));
    std::string wire;
    Q4T_CHECK(Ok(run.Begin(&wire)) &&
              run.Append(SearchDelta(0, arguments), {}, &wire));
    bool waiting;
    auto result = run.RunToolSearches(1, &waiting, Silent);
    if (schema == "false") {
      Q4T_CHECK(result.code == "tool_search_arguments_invalid" && !waiting);
      ResponsesExecution::ObserveError(&run, 502, "Invalid generated arguments",
                                       result.code);
      Q4T_CHECK(Ok(run.CloseUnfinished()));
    } else {
      Q4T_CHECK(
          Ok(result) && waiting &&
          OutputJson(*run.Output().Snapshot().GetArray("output")->array[0].Find(
              "arguments")) == arguments);
      Q4T_CHECK(Ok(run.Finish(false, 1, {10, 0, 10, 0}, 44, &wire)));
    }
  }
  return true;
}

Q4T_TEST(responses_discovery_real_parser_receipt_and_sse_items_are_consistent) {
  Fixture fixture;
  ObjectStore objects(fixture.options);
  ResponsesExecution run(objects);
  Q4T_CHECK(Ok(run.Prepare(Request(), Config(), Access(), 42)));
  std::string wire, full;
  Q4T_CHECK(Ok(run.Begin(&wire)));
  full += wire;
  const auto& request = run.Generation().generation_request;
  const auto& function = *request.GetArray("tools")->array[0].Find("function");
  ChatResponseParser parser;
  Q4T_CHECK(parser.Init(request,
                        "<|im_start|>assistant\n<think>\n\n</think>\n\n",
                        "cpu_discovery", false));
  const auto public_arguments = Read(R"({"paths":["project"]})");
  const auto arguments =
      Object({{"arguments_json", String(OutputJson(public_arguments))}});
  SchemaReceipt receipt;
  Q4T_CHECK(MakeSchemaReceipt(function.GetString("name"),
                              *function.Find("parameters"), arguments,
                              &receipt));
  std::vector<SchemaReceipt> receipts{receipt};
  parser.SetSchemaReceipts(&receipts);
  const auto text = "<tool_call>\n<function=" + function.GetString("name") +
                    ">\n<parameter=arguments_json>\n" +
                    OutputJson(public_arguments) +
                    "\n</parameter>\n</function>\n</tool_call>";
  for (const auto c : text) {
    std::vector<ResponseDelta> deltas;
    Q4T_CHECK(parser.Append(std::string(1, c), &deltas));
    for (const auto& delta : deltas) {
      Q4T_CHECK(run.Append(delta, "null", &wire));
      full += wire;
    }
  }
  std::vector<ResponseDelta> deltas;
  Q4T_CHECK(parser.Finish(false, &deltas));
  for (const auto& delta : deltas) {
    Q4T_CHECK(run.Append(delta, "null", &wire));
    full += wire;
  }
  bool waiting;
  Q4T_CHECK(Ok(run.RunToolSearches(parser.CompletedToolCalls(), &waiting,
                                   [&](const auto& part) {
                                     full += part;
                                     return true;
                                   })) &&
            !waiting);
  Q4T_CHECK(
      full.find("function_call_arguments") == full.npos &&
      full.find("q4t_builtin_tool_search") == full.npos &&
      OutputJson(*run.Output().Snapshot().GetArray("output")->array[0].Find(
          "arguments")) == R"({"paths":["project"]})");
  Q4T_CHECK(Ok(run.Finish(false, parser.CompletedToolCalls(), {10, 0, 10, 0},
                          44, &wire)));
  full += wire;
  const auto root =
      std::filesystem::path(std::getenv("Q4T_TEST_OUTPUT_DIR")) / "responses";
  std::filesystem::create_directories(root);
  std::ofstream(root / "discovery-parser.json")
      << OutputJson(run.Output().Snapshot()) << '\n';
  std::ofstream(root / "discovery-parser.wire") << full;
  return true;
}

Q4T_TEST(responses_discovery_loaded_async_function_and_custom_continue_once) {
  for (bool custom : {false, true}) {
    Fixture fixture;
    ObjectStore objects(fixture.options);
    auto request = Request();
    auto& tool = Field(Field(request, "tools").array[1], "tools").array[0];
    Set(&tool, "async", Boolean(true));
    if (custom) {
      Set(&tool, "type", String("custom"));
      std::erase_if(tool.object, [](const auto& field) {
        return field.first == "parameters" || field.first == "strict" ||
               field.first == "output_schema";
      });
    }
    ResponsesExecution run(objects);
    Q4T_CHECK(Ok(run.Prepare(request, Config(), Access(), 42)));
    std::string wire;
    Q4T_CHECK(Ok(run.Begin(&wire)) && run.Append(SearchDelta(), {}, &wire));
    bool waiting;
    Q4T_CHECK(Ok(run.RunToolSearches(1, &waiting, Silent)) && !waiting);
    ResponsesGenerationPlan next;
    Q4T_CHECK(Ok(run.ContinueFileSearch(10, Config(), {}, &next)) &&
              run.HasAsyncTools());
    ResponseDelta call;
    call.kind = ResponseDelta::Kind::kTool;
    call.tool_start = true;
    call.call = {0, "late",
                 ResponsesToolModelName("project", "read",
                                        custom ? "custom" : "function"),
                 custom ? "docs/MODEL.md" : R"({"path":"docs/MODEL.md"})",
                 custom};
    Q4T_CHECK(run.Append(call, {}, &wire));
    bool answer;
    Q4T_CHECK(Ok(run.ContinueAsyncTools(1, 20, Config(), {}, &next, &answer,
                                        &wire)) &&
              answer &&
              next.generation_request.GetString("tool_choice") == "none" &&
              next.generation_request.GetInt("max_completion_tokens") == 108);
    std::string prompt;
    ChatPlanError error;
    Q4T_CHECK(Ok(Prompt(next, &prompt, &error), error));
    Q4T_CHECK(prompt.find("private_loaded_description") >
                  prompt.find("<tool_response>") &&
              prompt.find("private_loaded_description") != prompt.npos);
    ResponsesSummaryPlan notes;
    Q4T_CHECK(
        Ok(PlanResponsesDeliberation(next, 128, 20, Config(), &notes, &error),
           error));
    Q4T_CHECK(notes.chat.header_tools &&
              OutputJson(*notes.chat.header_tools) ==
                  OutputJson(*next.chat.header_tools) &&
              notes.chat.tool_call_formats.size() ==
                  next.chat.tool_call_formats.size());
    ResponsesGenerationPlan final_answer;
    Q4T_CHECK(
        Ok(PlanResponsesDeliberatedAnswer(next, "Actual working notes", 128, 24,
                                          Config(), &final_answer, &error),
           error));
    Q4T_CHECK(Ok(Prompt(final_answer, &prompt, &error), error) &&
              prompt.find("private_loaded_description") >
                  prompt.find("<tool_response>"));
    Q4T_CHECK(Ok(run.ContinueAsyncTools(1, 20, Config(), {}, &next, &answer,
                                        &wire)) &&
              !answer);
    Q4T_CHECK(Ok(run.Finish(false, 1, {20, 0, 20, 0}, 44, &wire)));
  }
  return true;
}

Q4T_TEST(responses_discovery_server_parameters_constrain_paths_and_keep_refs) {
  ResponsesToolCatalog catalog;
  catalog.search = Read(R"({"type":"tool_search","execution":"server",
    "parameters":{"$defs":{"path":{"type":"string","const":"project"}},
      "type":"object","properties":{"paths":{"type":"array",
      "items":{"$ref":"#/$defs/path"},"minItems":1,"maxItems":1}},
      "required":["paths"],"additionalProperties":false}})");
  auto schema = ResponsesToolSearchArgumentsSchema(catalog);
  OutputSchemaGraph graph;
  Q4T_CHECK(graph.Init(schema, false, nullptr, "schema",
                       OutputSchemaPurpose::kToolResult));
  Q4T_CHECK(graph.Check(Read(R"({"paths":["project"]})")) &&
            !graph.Check(Read(R"({"paths":["different"]})")) &&
            !graph.Check(Read(R"({"paths":["project"],"extra":true})")));
  Json definition;
  ChatPlanError error;
  Q4T_CHECK(
      Ok(NativeResponsesToolSearchDefinition(catalog, &definition, &error),
         error));
  return true;
}

Q4T_TEST(
    responses_discovery_conversation_append_binds_previous_loaded_contract) {
  Fixture fixture;
  ObjectStore objects(fixture.options);
  ConversationState conversations(objects);
  ResponsesState inputs(objects);
  auto definitions = Read(R"({"type":"additional_tools","role":"developer",
    "tools":[{"type":"function","name":"read","async":true,
      "output_schema":{"type":"integer"}}]})");
  auto resolved = inputs.ResolveItems(Array({definitions}), Access());
  Q4T_CHECK(Ok(resolved));
  auto created = conversations.Create(Object(), resolved.value, Access());
  Q4T_CHECK(Ok(created));
  const auto id = created.value.GetString("id");
  resolved = inputs.ResolveItems(Read(R"([
    {"type":"function_call","call_id":"early","name":"read",
     "arguments":"{}","async":true}])"),
                                 Access());
  Q4T_CHECK(Ok(resolved) &&
            Ok(conversations.Append(id, resolved.value, {}, Access())));
  Set(&Field(definitions, "tools").array[0], "output_schema",
      Read(R"({"type":"string"})"));
  resolved = inputs.ResolveItems(Array({definitions}), Access());
  Q4T_CHECK(Ok(resolved) &&
            Ok(conversations.Append(id, resolved.value, {}, Access())));
  auto request = Read(R"({"reasoning":{"effort":"none"},"input":[
    {"type":"function_call_output","call_id":"early","output":"7"}]})");
  Set(&request, "conversation", String(id));
  ResponsesExecution accepted(objects);
  Q4T_CHECK(Ok(accepted.Prepare(request, Config(), Access(), 42)));
  Set(&Field(request, "input").array[0], "output",
      String("\"wrong original schema\""));
  ResponsesExecution rejected(objects);
  Q4T_CHECK(rejected.Prepare(request, Config(), Access(), 42).code ==
            "invalid_tool_output");
  return true;
}

Q4T_TEST(responses_discovery_cancel_and_reserved_user_name_cannot_dispatch) {
  Fixture fixture;
  ObjectStore objects(fixture.options);
  auto request = Request();
  Field(request, "tools")
      .array.push_back(Object(
          {{"type", String("function")},
           {"name", String(std::string(kResponsesToolSearchModelName))}}));
  ResponsesExecution run(objects);
  Q4T_CHECK(Ok(run.Prepare(request, Config(), Access(), 42)));
  const auto model_name =
      ResponsesToolModelName("", kResponsesToolSearchModelName, "function");
  Q4T_CHECK(model_name != kResponsesToolSearchModelName &&
            OutputJson(run.Generation().generation_request).find(model_name) !=
                std::string::npos);
  std::string wire;
  Q4T_CHECK(Ok(run.Begin(&wire)) && run.Append(SearchDelta(), {}, &wire));
  run.BindCancellation(Clock::now() + 5s, [] { return true; });
  bool waiting = false;
  size_t emitted = 0;
  Q4T_CHECK(run.RunToolSearches(1, &waiting,
                                [&](const auto&) {
                                  ++emitted;
                                  return true;
                                })
                    .http_status == 499 &&
            emitted == 0 && !waiting);
  run.BindCancellation(Clock::now() + 5s, {});
  ResponsesExecution::ObserveError(&run, 499, "Fixture cleanup",
                                   "request_cancelled");
  Q4T_CHECK(Ok(run.CloseUnfinished()));
  return true;
}

Q4T_TEST(
    responses_discovery_native_adapter_preserves_schema_refs_and_exact_values) {
  ResponsesToolCatalog catalog;
  catalog.search = Read(R"({"type":"tool_search","execution":"client",
    "parameters":{"$defs":{"value":{"type":"string","const":"keep"}},
      "type":"object","properties":{"x":{"$ref":"#/$defs/value"},
      "data":{"const":{"$ref":"literal_data"}}},"required":["x","data"],
      "additionalProperties":false}})");
  catalog.summaries = Array();
  Json definition;
  ChatPlanError error;
  Q4T_CHECK(
      Ok(NativeResponsesToolSearchDefinition(catalog, &definition, &error),
         error));
  const auto& parameters = *definition.Find("function")->Find("parameters");
  Q4T_CHECK(definition.Find("function")->GetBool("strict") &&
            ValidateOutputSchema(parameters, true));
  const auto text = OutputJson(parameters);
  Q4T_CHECK(text.find("literal_data") != text.npos &&
            text.find("#/$defs/value") != text.npos);
  OutputSchemaGraph graph;
  Q4T_CHECK(graph.Init(*catalog.search.Find("parameters"), false, nullptr,
                       "schema", OutputSchemaPurpose::kToolResult));
  Q4T_CHECK(
      graph.Check(Read(R"({"x":"keep","data":{"$ref":"literal_data"}})")));
  Q4T_CHECK(
      !graph.Check(Read(R"({"x":"changed","data":{"$ref":"literal_data"}})")));
  Json arguments;
  Q4T_CHECK(DecodeResponsesToolSearchArguments(
      OutputJson(
          Object({{"arguments_json", String("[9007199254740993,1e-500]")}})),
      &arguments));
  Q4T_CHECK(OutputJson(arguments) == "[9007199254740993,1e-500]");
  for (const auto* bad : {"[]", "{}", R"({"arguments_json":{},"extra":1})",
                          R"({"arguments_json":"invalid json"})"})
    Q4T_CHECK(!DecodeResponsesToolSearchArguments(bad, &arguments));
  return true;
}

Q4T_TEST(responses_discovery_changes_constraints_only_after_ordered_result) {
  auto request = Request();
  ResponsesGenerationPlan first, next;
  ChatPlanError error;
  Q4T_CHECK(Ok(Plan(request, &first, &error), error));
  const auto name = ResponsesToolModelName("project", "read", "function");
  Q4T_CHECK(first.generation_request.GetArray("tools")->array.size() == 1 &&
            OutputJson(first.generation_request).find(name) ==
                std::string::npos);
  Set(&request, "tool_choice",
      Read(R"({"type":"function","name":"project.read"})"));
  Q4T_CHECK(!Plan(request, &next, &error));
  Set(&request, "tool_choice", String("auto"));
  Set(&request, "input",
      Array({Read(R"({"role":"user","content":"Find docs"})"), SearchCall(),
             SearchResult()}));
  Q4T_CHECK(Ok(Plan(request, &next, &error), error));
  Q4T_CHECK(next.generation_request.GetArray("tools")->array.size() == 2);
  std::string prompt;
  Q4T_CHECK(Ok(Prompt(next, &prompt, &error), error));
  const auto loaded = prompt.find("private_loaded_description");
  Q4T_CHECK(loaded != prompt.npos && loaded > prompt.find("Find docs") &&
            loaded > prompt.find("<tool_response>"));
  Q4T_CHECK(OutputJson(next.effective_tools) ==
            OutputJson(first.effective_tools));
  Set(&request, "tool_choice",
      Read(R"({"type":"function","name":"project.read"})"));
  Q4T_CHECK(Ok(Plan(request, &next, &error), error));
  return true;
}

Q4T_TEST(responses_discovery_redefinition_does_not_rewrite_prior_call_prefix) {
  auto request = Read(R"({"reasoning":{"effort":"none"},"tools":[
    {"type":"tool_search"},{"type":"function","name":"read","strict":false,
      "parameters":{"type":"object","properties":{"path":{"type":"string"}},"additionalProperties":false}}],
    "input":[{"role":"user","content":"Read docs"},
      {"type":"function_call","call_id":"old","name":"read","arguments":"{\"path\":\"docs/MODEL.md\"}"},
      {"type":"function_call_output","call_id":"old","output":"old result"}]})");
  ResponsesGenerationPlan first, next;
  ChatPlanError error;
  Q4T_CHECK(Ok(Plan(request, &first, &error), error));
  std::string before, after;
  Q4T_CHECK(Ok(Prompt(first, &before, &error), error));
  Field(request, "input").array.push_back(Read(R"({"type":"additional_tools",
    "role":"developer","tools":[{"type":"function","name":"read",
      "parameters":{"type":"object","$defs":{"path":{"type":"string"}},
        "properties":{"path":{"$ref":"#/$defs/path"}},"required":["path"],
        "additionalProperties":false},"strict":true}]})"));
  Q4T_CHECK(Ok(Plan(request, &next, &error), error));
  Q4T_CHECK(Ok(Prompt(next, &after, &error), error));
  const auto boundary = before.find("old result");
  Q4T_CHECK(boundary != before.npos &&
            before.substr(0, boundary) == after.substr(0, boundary) &&
            after.find("<parameter=path>") < boundary);
  return true;
}

Q4T_TEST(
    responses_discovery_server_executes_persists_and_binds_loaded_function) {
  Fixture fixture;
  ObjectStore objects(fixture.options);
  ResponsesExecution run(objects);
  Q4T_CHECK(Ok(run.Prepare(Request(), Config(), Access(), 42)) &&
            run.HasToolSearch() && run.VariableInputSteps());
  std::string wire, full;
  Q4T_CHECK(Ok(run.Begin(&wire)) && run.Append(SearchDelta(), {}, &wire));
  full += wire;
  bool waiting = true;
  Q4T_CHECK(Ok(run.RunToolSearches(1, &waiting,
                                   [&](const auto& part) {
                                     full += part;
                                     return true;
                                   })) &&
            !waiting && !run.PendingToolSearch());
  Q4T_CHECK(full.find("response.function_call_arguments") == full.npos &&
            full.find("response.output_item.done") != full.npos);
  const auto& items = run.Output().Snapshot().GetArray("output")->array;
  Q4T_CHECK(items.size() == 2 &&
            items[0].GetString("type") == "tool_search_call" &&
            items[0].Find("call_id")->IsNull() &&
            items[1].GetString("type") == "tool_search_output");
  for (const auto& item : items) {
    ChatPlanError error;
    Q4T_CHECK(ValidateResponsesItemShape(item, ResponsesItemShape::kOutput,
                                         &error, "output"));
  }
  ResponsesGenerationPlan next;
  Q4T_CHECK(Ok(run.ContinueFileSearch(12, Config(), {}, &next)) &&
            next.generation_request.GetInt("max_completion_tokens") == 116);
  ResponseDelta call;
  call.kind = ResponseDelta::Kind::kTool;
  call.tool_start = true;
  call.call = {0, "read_call",
               ResponsesToolModelName("project", "read", "function"),
               R"({"path":"docs/MODEL.md"})", false};
  Q4T_CHECK(run.Append(call, {}, &wire));
  Q4T_CHECK(Ok(run.Finish(false, 1, {30, 0, 20, 0}, 44, &wire)));
  const auto id = run.Id();
  auto follow = Read(R"({"input":[{"type":"function_call_output",
    "call_id":"read_call","output":"{\"title\":\"Model\"}"}],
    "reasoning":{"effort":"none"}})");
  Set(&follow, "previous_response_id", String(id));
  ResponsesExecution resumed(objects);
  Q4T_CHECK(Ok(resumed.Prepare(follow, Config(), Access(), 45)));
  Q4T_CHECK(OutputJson(resumed.Generation().active_tools)
                .find("private_loaded_description") != std::string::npos);
  Set(&Field(follow, "input").array[0], "output", String(R"({"title":5})"));
  ResponsesExecution invalid(objects);
  Q4T_CHECK(invalid.Prepare(follow, Config(), Access(), 45).code ==
            "invalid_tool_output");
  return true;
}

Q4T_TEST(
    responses_discovery_client_arbitrary_arguments_and_new_rest_definitions) {
  Fixture fixture;
  std::string id, call_id;
  {
    ObjectStore objects(fixture.options);
    auto request = Request();
    Set(&request, "tools", Read(R"([{"type":"tool_search","execution":"client",
      "parameters":{"type":"array","items":{"type":"integer"},"minItems":1,
      "maxItems":2}}])"));
    ResponsesExecution run(objects);
    Q4T_CHECK(Ok(run.Prepare(request, Config(), Access(), 42)));
    std::string wire;
    Q4T_CHECK(Ok(run.Begin(&wire)) &&
              run.Append(SearchDelta(0, R"([9007199254740993])"), {}, &wire));
    bool waiting = false;
    Q4T_CHECK(Ok(run.RunToolSearches(1, &waiting, Silent)) && waiting);
    Q4T_CHECK(Ok(run.Finish(false, 1, {10, 0, 8, 0}, 44, &wire)));
    const auto& item = run.Output().Snapshot().GetArray("output")->array[0];
    Q4T_CHECK(OutputJson(*item.Find("arguments")) == "[9007199254740993]" &&
              item.GetString("execution") == "client");
    call_id = item.GetString("call_id");
    id = run.Id();
  }
  ObjectStore objects(fixture.options);
  auto request = Read(R"({"background":true,"reasoning":{"effort":"none"},
    "input":[{"type":"tool_search_output","tools":[{"type":"function",
      "name":"newly_discovered"}]}]})");
  Set(&request, "previous_response_id", String(id));
  Set(&Field(request, "input").array[0], "call_id", String(call_id));
  ResponsesExecution queued(objects);
  Q4T_CHECK(Ok(queued.Prepare(request, Config(), Access(), 45, true)));
  Q4T_CHECK(
      OutputJson(queued.Generation().active_tools).find("newly_discovered") !=
      std::string::npos);
  std::string wire;
  Json frozen;
  Q4T_CHECK(Ok(queued.FreezeInputs(&frozen)));
  ResponsesExecution from_frozen(objects);
  Q4T_CHECK(Ok(from_frozen.RestoreInputs(frozen, Config(), Access(), 45)) &&
            OutputJson(from_frozen.Generation().active_tools) ==
                OutputJson(queued.Generation().active_tools));
  Q4T_CHECK(Ok(queued.Begin(&wire)));
  ResponsesExecution restored(objects);
  Q4T_CHECK(Ok(restored.RestoreQueued(queued.Id(), Config(), Access())) &&
            OutputJson(restored.Generation().active_tools) ==
                OutputJson(queued.Generation().active_tools));
  auto other = Access();
  other.scope = "client:other";
  ResponsesExecution forbidden(objects);
  Q4T_CHECK(!forbidden.Prepare(request, Config(), other, 45, true).ok());
  Q4T_CHECK(Ok(restored.Cancel()));
  return true;
}

Q4T_TEST(
    responses_discovery_result_schema_binds_at_invocation_not_final_catalog) {
  auto history = Read(R"([
    {"type":"additional_tools","role":"developer","tools":[
      {"type":"function","name":"read","output_schema":{"type":"integer"}}]},
    {"type":"function_call","id":"fc_first","call_id":"first","name":"read","arguments":"{}"},
    {"type":"function_call_output","call_id":"first","output":"7"},
    {"type":"additional_tools","role":"developer","tools":[
      {"type":"function","name":"read","output_schema":{"type":"string"}}]},
    {"type":"function_call","id":"fc_second","call_id":"second","name":"read","arguments":"{}"},
    {"type":"function_call_output","call_id":"second","output":"\"new\""}])");
  Json contracts;
  Q4T_CHECK(Ok(BindToolResultContracts(Object(), history, &contracts)) &&
            contracts.array.size() == 2 &&
            contracts.array[0].Find("schema")->GetString("type") == "integer" &&
            contracts.array[1].Find("schema")->GetString("type") == "string");
  Q4T_CHECK(
      Ok(CheckToolResultContracts(Object(), history, contracts, Access())));
  Set(&history.array[2], "output", String("\"wrong for original call\""));
  Q4T_CHECK(
      CheckToolResultContracts(Object(), history, contracts, Access()).code ==
      "invalid_tool_output");
  return true;
}

Q4T_TEST(
    responses_discovery_builtin_budget_closes_skipped_calls_without_loading) {
  Fixture fixture;
  ObjectStore objects(fixture.options);
  auto request = Request();
  Set(&request, "max_tool_calls", Number(1));
  ResponsesExecution run(objects);
  Q4T_CHECK(Ok(run.Prepare(request, Config(), Access(), 42)));
  std::string wire;
  Q4T_CHECK(Ok(run.Begin(&wire)) && run.Append(SearchDelta(), {}, &wire) &&
            run.Append(SearchDelta(1), {}, &wire));
  bool waiting;
  Q4T_CHECK(Ok(run.RunToolSearches(2, &waiting, Silent)) && !waiting);
  const auto& output = run.Output().Snapshot().GetArray("output")->array;
  Q4T_CHECK(output.size() == 3 &&
            output[0].GetString("status") == "completed" &&
            output[1].GetString("status") == "incomplete");
  ResponsesGenerationPlan next;
  Q4T_CHECK(Ok(run.ContinueFileSearch(16, Config(), {}, &next)));
  Q4T_CHECK(next.chat.constraints.structural_tag.find(
                "q4t_builtin_tool_search") == std::string::npos);
  ResponsesExecution::ObserveError(&run, 499, "Fixture cleanup",
                                   "request_cancelled");
  Q4T_CHECK(Ok(run.CloseUnfinished()));
  return true;
}

Q4T_TEST(responses_discovery_bad_or_unfinished_arguments_never_dispatch) {
  for (const auto* arguments :
       {R"({"paths":[9]})", R"({"paths":["project"]})"}) {
    Fixture fixture;
    ObjectStore objects(fixture.options);
    ResponsesExecution run(objects);
    Q4T_CHECK(Ok(run.Prepare(Request(), Config(), Access(), 42)));
    std::string wire;
    Q4T_CHECK(Ok(run.Begin(&wire)) &&
              run.Append(SearchDelta(0, arguments), {}, &wire));
    bool waiting;
    const bool malformed =
        std::string_view(arguments).find('9') != std::string_view::npos;
    auto result = run.RunToolSearches(malformed ? 1 : 0, &waiting, Silent);
    Q4T_CHECK(result.http_status == 502 && !waiting &&
              run.Output().Snapshot().GetArray("output")->array.size() == 1);
    ResponsesExecution::ObserveError(&run, 499, "Fixture cleanup",
                                     "request_cancelled");
    Q4T_CHECK(Ok(run.CloseUnfinished()));
  }
  return true;
}

Q4T_TEST(
    responses_discovery_truncation_preserves_definitions_and_original_paths) {
  auto request = Request();
  Set(&request, "truncation", String("auto"));
  Set(&request, "input",
      Array({Read(R"({"role":"user","content":"old bulky text"})"),
             SearchCall(), SearchResult(),
             Read(R"({"role":"user","content":"current"})")}));
  ResponsesEnvelope envelope;
  ResponsesGenerationPlan plan;
  ChatPlanError error;
  Q4T_CHECK(ParseResponsesEnvelope(request, Config(), &envelope, &error) &&
            Ok(Plan(request, &plan, &error), error));
  auto check = [](const ResponsesGenerationPlan& candidate,
                  ChatPlanError* error) {
    if (OutputJson(candidate.input.items).find("old bulky text") ==
        std::string::npos)
      return q4t::Status();
    *error = {"context_length_exceeded", "input"};
    return q4t::Status::Fail("Fixture context overflow");
  };
  Q4T_CHECK(
      Ok(FitResponsesContext(envelope, Config(), check, &plan, &error), error));
  Q4T_CHECK(plan.truncated_items == 1 && plan.input.items.array.size() == 3 &&
            plan.input.items.array[0].GetString("type") == "tool_search_call" &&
            plan.input.message_paths.back() == "input[3]" &&
            OutputJson(plan.active_tools).find("private_loaded_description") !=
                std::string::npos);
  return true;
}

Q4T_TEST(responses_discovery_compaction_keeps_executable_definitions_exact) {
  Fixture fixture;
  ObjectStore objects(fixture.options);
  StateCipher cipher(objects);
  auto history = Array(
      {Read(
           R"({"id":"msg_old","type":"message","role":"user","content":"old"})"),
       SearchCall(), SearchResult(),
       Read(
           R"({"id":"msg_answer","type":"message","role":"assistant","content":"done"})"),
       Read(
           R"({"id":"msg_current","type":"message","role":"user","content":"continue"})")});
  for (auto& message : history.array) {
    if (message.GetString("type") != "message") continue;
    auto part = Object({{"type", String(message.GetString("role") == "assistant"
                                            ? "output_text"
                                            : "input_text")},
                        {"text", *message.Find("content")}});
    if (message.GetString("role") == "assistant")
      Set(&part, "annotations", Array());
    Set(&message, "content", Array({std::move(part)}));
    Set(&message, "status", String("completed"));
  }
  ResponsesCompactionPlan plan;
  ChatPlanError error;
  Q4T_CHECK(
      Ok(PlanResponsesCompaction(history, ResponsesCompactionMode::kAutomatic,
                                 32, Config(), &plan, &error),
         error));
  Q4T_CHECK(plan.compacted_items == 2 && plan.retained.array.size() == 3 &&
            OutputJson(plan.retained.array[0]) ==
                OutputJson(history.array[1]) &&
            OutputJson(plan.retained.array[1]) == OutputJson(history.array[2]));
  Json item, expanded, opened;
  Q4T_CHECK(
      Ok(SealResponsesCompaction(cipher, plan, "Earlier task done", "cmp_tools",
                                 Config(), Access(), &item, &expanded)));
  Q4T_CHECK(Ok(OpenResponsesCompaction(cipher, Array(), item, Config(),
                                       Access(), &opened)) &&
            OutputJson(opened) == OutputJson(expanded));
  auto request = Request();
  Set(&request, "input", opened);
  ResponsesGenerationPlan next;
  Q4T_CHECK(Ok(Plan(request, &next, &error), error) &&
            OutputJson(next.active_tools).find("private_loaded_description") !=
                std::string::npos);
  return true;
}

Q4T_TEST(
    responses_discovery_input_count_matches_generation_without_prefix_leak) {
  Fixture fixture;
  ObjectStore objects(fixture.options);
  auto request = Request();
  Set(&request, "input",
      Array({Read(R"({"role":"user","content":"Find docs"})"), SearchCall(),
             SearchResult()}));
  auto count_request = request;
  std::erase_if(count_request.object, [](const auto& field) {
    return field.first == "max_output_tokens";
  });
  ResponsesEnvelope envelope;
  ResponsesGenerationPlan count, generation;
  auto counting = Config();
  counting.count_input_only = true;
  Q4T_CHECK(Ok(PrepareResponsesTokenCount(count_request, counting, objects,
                                          Access(), &envelope, &count)));
  ChatPlanError error;
  Q4T_CHECK(Ok(Plan(request, &generation, &error), error));
  std::string before, after;
  Q4T_CHECK(Ok(Prompt(count, &before, &error), error) &&
            Ok(Prompt(generation, &after, &error), error) && before == after);
  return true;
}
