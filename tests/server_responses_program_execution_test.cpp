#include "q4t/server/responses_execution.h"

#include <cstdlib>
#include <filesystem>
#include <fstream>
#include <stdexcept>

#include "../src/server/object_protocol.h"
#include "q4t/server/chat_prompt.h"
#include "q4t/server/conversation_state.h"
#include "q4t/server/responses_item_shape.h"
#include "q4t/server/responses_tokens.h"
#include "q4t/test.h"

namespace {
using namespace q4t::server;
using namespace q4t::server::object_detail;
using Clock = std::chrono::steady_clock;
using namespace std::chrono_literals;
Json Parse(const std::string& text) {
  Json value;
  if (!q4t::io::ParseJson(text, &value, true))
    throw std::runtime_error("fixture");
  return value;
}
bool Ok(const ObjectReply& result) {
  if (!result.ok())
    std::fprintf(stderr, "%d %s %s: %s\n", result.http_status,
                 result.code.c_str(), result.value.GetString("param").c_str(),
                 result.message.c_str());
  return result.ok();
}
struct Fixture {
  ObjectStoreOptions options;
  Fixture() {
    auto* root = std::getenv("Q4T_TEST_OUTPUT_DIR");
    if (!root) throw std::runtime_error("fixture root");
    auto path = (std::filesystem::path(root) / "program-api-XXXXXX").string();
    if (!mkdtemp(path.data())) throw std::runtime_error("fixture directory");
    options.directory = path + "/objects";
    options.restart_backoff_ms = 0;
  }
};
StateAccess Access() { return {"client:program-api", Clock::now() + 10s, {}}; }
ChatPlanConfig Config() {
  ChatPlanConfig config;
  config.model = "cpu";
  config.structured = true;
  return config;
}
Json Request() {
  return Parse(R"({"input":"Read the project value and double it",
    "reasoning":{"effort":"none"},"max_output_tokens":128,
    "tools":[{"type":"programmatic_tool_calling"},
     {"type":"function","name":"read","allowed_callers":["programmatic"],
      "parameters":{"type":"object","properties":{"path":{"type":"string"}},
        "required":["path"],"additionalProperties":false},"strict":true,
      "output_schema":{"type":"object","properties":{"value":{"type":"integer"}},
        "required":["value"],"additionalProperties":false}}]})");
}
ResponseDelta Code(std::string code, size_t index = 0) {
  ResponseDelta delta;
  delta.kind = ResponseDelta::Kind::kTool;
  delta.tool_start = true;
  delta.call = {index, "generated_program_" + std::to_string(index),
                kResponsesProgramModelName,
                OutputJson(Object({{"code", String(std::move(code))}})), false};
  return delta;
}
ResponseDelta Text(std::string text) {
  ResponseDelta delta;
  delta.kind = ResponseDelta::Kind::kContent;
  delta.text = std::move(text);
  return delta;
}
bool Silent(const std::string&) { return true; }
Json Pending(ObjectStore& objects, Json request = Request(),
             std::string code =
                 "const r = await tools.read({path:'docs/MODEL.md'}); "
                 "text(r.value * 2);") {
  ResponsesExecution run(objects);
  if (!Ok(run.Prepare(request, Config(), Access(), 42)))
    throw std::runtime_error("prepare program API");
  std::string wire;
  bool waiting = false;
  if (!Ok(run.Begin(&wire)) || !run.Append(Code(std::move(code)), {}, &wire) ||
      !Ok(run.RunPrograms(1, &waiting, Silent)) || !waiting ||
      !Ok(run.Finish(false, 1, {11, 0, 19, 0}, 44, &wire)))
    throw std::runtime_error("execute program API");
  return run.PublicResponse();
}
Json Result(const Json& response, std::string output = "{\"value\":21}") {
  const auto& call = response.GetArray("output")->array.at(1);
  return Object({{"type", String("function_call_output")},
                 {"call_id", *call.Find("call_id")},
                 {"caller", *call.Find("caller")},
                 {"output", String(std::move(output))}});
}
}  // namespace

Q4T_TEST(
    responses_program_api_routes_and_choice_preserve_only_authorized_calls) {
  auto request = Request();
  Set(&request, "tool_choice",
      Object({{"type", String("programmatic_tool_calling")}}));
  Json tools, choice, effective;
  ChatPlanError error;
  Q4T_CHECK(
      NormalizeResponsesTools(request, &tools, &choice, &error, &effective));
  Q4T_CHECK(tools.array.size() == 1 &&
            tools.array[0].Find("function")->GetString("name") ==
                kResponsesProgramModelName &&
            choice.Find("function")->GetString("name") ==
                kResponsesProgramModelName &&
            effective.array.size() == 2);
  Set(&request, "tool_choice", Parse(R"({"type":"function","name":"read"})"));
  Q4T_CHECK(!NormalizeResponsesTools(request, &tools, &choice, &error));
  Set(&Field(request, "tools").array[1], "allowed_callers",
      Parse(R"(["direct","programmatic"])"));
  Q4T_CHECK(NormalizeResponsesTools(request, &tools, &choice, &error) &&
            tools.array.size() == 2);
  auto& definitions = Field(request, "tools").array;
  definitions.push_back(definitions.front());
  Q4T_CHECK(!NormalizeResponsesTools(request, &tools, &choice, &error));
  return true;
}

Q4T_TEST(
    responses_program_api_direct_output_cannot_bypass_program_only_policy) {
  Fixture f;
  ObjectStore objects(f.options);
  ResponsesExecution run(objects);
  Q4T_CHECK(Ok(run.Prepare(Request(), Config(), Access(), 42)));
  std::string wire;
  Q4T_CHECK(Ok(run.Begin(&wire)));
  auto direct = Code("unused");
  direct.call.name = "read";
  direct.call.arguments = "{\"path\":\"docs/MODEL.md\"}";
  Q4T_CHECK(!run.Append(direct, {}, &wire) && wire.empty());
  ResponsesExecution::ObserveError(&run, 502, "Invalid direct route",
                                   "invalid_tool");
  Q4T_CHECK(Ok(run.CloseUnfinished()));
  return true;
}

Q4T_TEST(responses_program_api_stored_resume_sse_and_final_answer) {
  Fixture f;
  ObjectStore objects(f.options);
  const auto pending = Pending(objects);
  Q4T_CHECK(pending.GetArray("output")->array.size() == 2);
  auto request = Request();
  Set(&request, "previous_response_id", *pending.Find("id"));
  Set(&request, "input", Array({Result(pending)}));
  Set(&request, "stream", Boolean(true));
  Set(&request, "stream_options",
      Object({{"include_obfuscation", Boolean(false)}}));
  ResponsesExecution resumed(objects);
  Q4T_CHECK(Ok(resumed.Prepare(request, Config(), Access(), 45)) &&
            resumed.PendingProgramResume());
  std::string wire, full;
  Q4T_CHECK(Ok(resumed.Begin(&wire)));
  full += wire;
  bool waiting;
  Q4T_CHECK(Ok(resumed.RunPrograms(0, &waiting,
                                   [&](const std::string& part) {
                                     full += part;
                                     return true;
                                   })) &&
            !waiting && !resumed.PendingProgramResume());
  ResponsesGenerationPlan next;
  Q4T_CHECK(Ok(resumed.ContinueFileSearch(0, Config(), {}, &next)));
  const auto text = OutputJson(next.input.messages);
  Q4T_CHECK(text.find("42") != std::string::npos &&
            text.find("fingerprint") == std::string::npos &&
            text.find("value\\\":21") == std::string::npos);
  Q4T_CHECK(
      resumed.Append(Text("The project value doubled is 42."), {}, &wire));
  full += wire;
  Q4T_CHECK(Ok(resumed.Finish(false, 0, {19, 0, 9, 0}, 46, &wire)));
  full += wire;
  const auto response = resumed.PublicResponse();
  Q4T_CHECK(response.GetArray("output")->array[0].GetString("type") ==
                "program_output" &&
            response.GetArray("output")->array[0].GetString("result") == "42");
  auto get =
      ResponsesState(objects).Retrieve(response.GetString("id"), Access());
  Q4T_CHECK(Ok(get) &&
            get.value.GetArray("output")->array[0].GetString("result") == "42");
  const auto root = std::filesystem::path(std::getenv("Q4T_TEST_OUTPUT_DIR")) /
                    "program-api-wire";
  std::filesystem::create_directories(root);
  std::ofstream(root / "pending.json") << OutputJson(pending) << '\n';
  std::ofstream(root / "response.json") << OutputJson(response) << '\n';
  std::ofstream(root / "response.wire") << resumed.PublicWire(full);
  return true;
}

Q4T_TEST(
    responses_program_api_stateless_and_changed_schema_keep_original_authority) {
  Fixture f;
  ObjectStore objects(f.options);
  auto first = Request();
  Set(&first, "store", Boolean(false));
  const auto pending = Pending(objects, first);
  auto history = Array(
      {Object({{"role", String("user")}, {"content", *first.Find("input")}})});
  for (const auto& item : pending.GetArray("output")->array)
    history.array.push_back(item);
  history.array.push_back(Result(pending, "{\"value\":\"wrong\"}"));
  auto request = Request();
  Set(&Field(request, "tools").array[1], "output_schema", Object());
  Set(&request, "input", history);
  ResponsesExecution rejected(objects);
  Q4T_CHECK(rejected.Prepare(request, Config(), Access(), 45).code ==
            "invalid_tool_output");
  Set(&history.array.back(), "output", String("{\"value\":5}"));
  Set(&request, "input", history);
  ResponsesExecution valid(objects);
  Q4T_CHECK(Ok(valid.Prepare(request, Config(), Access(), 46)));
  std::string wire;
  bool waiting;
  Q4T_CHECK(Ok(valid.Begin(&wire)) &&
            Ok(valid.RunPrograms(0, &waiting, Silent)) && !waiting);
  Q4T_CHECK(valid.Output().Snapshot().GetArray("output")->array[0].GetString(
                "result") == "10");
  Q4T_CHECK(Ok(valid.Finish(false, 0, {}, 47, &wire)));
  Set(&history.array[1], "code", String("text('forged')"));
  Set(&request, "input", history);
  ResponsesExecution forged(objects);
  Q4T_CHECK(!forged.Prepare(request, Config(), Access(), 48).ok());
  return true;
}

Q4T_TEST(responses_program_api_truncated_code_never_dispatches) {
  Fixture f;
  ObjectStore objects(f.options);
  ResponsesExecution run(objects);
  Q4T_CHECK(Ok(run.Prepare(Request(), Config(), Access(), 42)));
  std::string wire;
  Q4T_CHECK(Ok(run.Begin(&wire)) &&
            run.Append(Code("await tools.read({path:'x'});"), {}, &wire));
  bool waiting;
  Q4T_CHECK(run.RunPrograms(0, &waiting, Silent).code == "program_incomplete");
  Q4T_CHECK(run.Output().Snapshot().GetArray("output")->array.empty() &&
            !run.PendingPrograms());
  Q4T_CHECK(!run.RunPrograms(1, &waiting, Silent).ok());
  ResponsesExecution::ObserveError(&run, 502, "Truncated",
                                   "program_incomplete");
  Q4T_CHECK(Ok(run.CloseUnfinished()));
  return true;
}

Q4T_TEST(responses_program_api_no_tool_program_continues_under_shared_budget) {
  Fixture f;
  ObjectStore objects(f.options);
  auto request = Request();
  Set(&request, "max_tool_calls", Number(1));
  ResponsesExecution run(objects);
  Q4T_CHECK(Ok(run.Prepare(request, Config(), Access(), 42)));
  std::string wire;
  bool waiting;
  Q4T_CHECK(Ok(run.Begin(&wire)) && run.Append(Code("text(6*7);"), {}, &wire) &&
            Ok(run.RunPrograms(1, &waiting, Silent)) && !waiting);
  ResponsesGenerationPlan next;
  Q4T_CHECK(Ok(run.ContinueFileSearch(12, Config(), {}, &next)));
  Q4T_CHECK(next.generation_request.GetInt("max_completion_tokens") == 116 &&
            next.generation_request.GetArray("tools")->array.empty());
  Q4T_CHECK(run.Append(Text("42"), {}, &wire) &&
            Ok(run.Finish(false, 1, {5, 0, 13, 0}, 44, &wire)));
  const auto response = run.PublicResponse();
  Q4T_CHECK(response.GetArray("output")->array.size() == 3 &&
            response.GetArray("output")->array[1].GetString("result") == "42");
  return true;
}

Q4T_TEST(responses_program_api_frozen_queue_preserves_program_result_contract) {
  Fixture f;
  ObjectStore objects(f.options);
  const auto pending = Pending(objects);
  auto request = Request();
  Set(&request, "previous_response_id", *pending.Find("id"));
  Set(&request, "input", Array({Result(pending)}));
  ResponsesExecution prepare(objects);
  Q4T_CHECK(Ok(prepare.Prepare(request, Config(), Access(), 45)));
  Json frozen;
  Q4T_CHECK(Ok(prepare.FreezeInputs(&frozen)));
  ResponsesExecution restore(objects);
  Q4T_CHECK(Ok(restore.RestoreInputs(frozen, Config(), Access(), 46)) &&
            restore.PendingProgramResume());
  std::string wire;
  bool waiting;
  Q4T_CHECK(Ok(restore.Begin(&wire)) &&
            Ok(restore.RunPrograms(0, &waiting, Silent)) && !waiting &&
            Ok(restore.Finish(false, 0, {}, 47, &wire)));
  Q4T_CHECK(restore.PublicResponse().GetArray("output")->array[0].GetString(
                "result") == "42");
  return true;
}

Q4T_TEST(responses_program_api_output_review_holds_client_dispatch) {
  Fixture f;
  ObjectStore objects(f.options);
  ResponsesExecution run(objects);
  Q4T_CHECK(Ok(run.Prepare(Request(), Config(), Access(), 42)));
  std::string wire;
  Q4T_CHECK(Ok(run.Begin(&wire)) && Ok(run.HoldUnreviewedOutput()));
  Q4T_CHECK(run.Append(Code("text(await tools.read({path:'docs/MODEL.md'}));"),
                       {}, &wire));
  bool waiting;
  std::string published;
  Q4T_CHECK(Ok(run.RunPrograms(1, &waiting,
                               [&](const std::string& part) {
                                 published += part;
                                 return true;
                               })) &&
            waiting && published.empty());
  auto get = ResponsesState(objects).Retrieve(run.Id(), Access());
  Q4T_CHECK(Ok(get) && get.value.GetArray("output")->array.empty());
  run.DiscardUnreviewedOutput();
  Q4T_CHECK(Ok(run.Finish(true, 0, {}, 44, &wire, true)));
  Q4T_CHECK(run.PublicResponse().GetArray("output")->array.empty());
  return true;
}

Q4T_TEST(responses_program_api_serial_promise_all_keeps_sealed_policy) {
  Fixture f;
  ObjectStore objects(f.options);
  auto request = Request();
  Set(&request, "parallel_tool_calls", Boolean(false));
  const auto first =
      Pending(objects, request,
              "const r = await Promise.all([tools.read({path:'a'}), "
              "tools.read({path:'b'})]); text(r[0].value+r[1].value);");
  Q4T_CHECK(first.GetArray("output")->array.size() == 2);
  Set(&request, "previous_response_id", *first.Find("id"));
  Set(&request, "parallel_tool_calls", Boolean(true));
  Set(&request, "input", Array({Result(first, "{\"value\":3}")}));
  ResponsesExecution second(objects);
  std::string wire;
  bool waiting;
  Q4T_CHECK(Ok(second.Prepare(request, Config(), Access(), 45)) &&
            Ok(second.Begin(&wire)) &&
            Ok(second.RunPrograms(0, &waiting, Silent)) && waiting &&
            Ok(second.Finish(false, 0, {}, 46, &wire)));
  const auto output = second.PublicResponse();
  Q4T_CHECK(output.GetArray("output")->array.size() == 1);
  const auto& call = output.GetArray("output")->array[0];
  Q4T_CHECK(call.GetString("arguments") == "{\"path\":\"b\"}");
  auto result = Result(first, "{\"value\":4}");
  Set(&result, "call_id", *call.Find("call_id"));
  Set(&request, "input", Array({result}));
  Set(&request, "previous_response_id", *output.Find("id"));
  ResponsesExecution third(objects);
  Q4T_CHECK(Ok(third.Prepare(request, Config(), Access(), 47)) &&
            Ok(third.Begin(&wire)) &&
            Ok(third.RunPrograms(0, &waiting, Silent)) && !waiting &&
            Ok(third.Finish(false, 0, {}, 48, &wire)));
  Q4T_CHECK(third.PublicResponse().GetArray("output")->array[0].GetString(
                "result") == "7");
  return true;
}

Q4T_TEST(
    responses_program_api_conversation_restart_and_compaction_keep_authority) {
  Fixture f;
  Json pending, conversation;
  {
    ObjectStore objects(f.options);
    auto created =
        ConversationState(objects).Create(Object(), Array(), Access());
    Q4T_CHECK(Ok(created));
    conversation = *created.value.Find("id");
    auto request = Request();
    Set(&request, "conversation", conversation);
    pending = Pending(objects, request);
  }
  ObjectStore restarted(f.options);
  auto request = Request();
  Set(&request, "conversation", conversation);
  Set(&request, "input", Array({Result(pending)}));
  ResponsesExecution resumed(restarted);
  Q4T_CHECK(Ok(resumed.Prepare(request, Config(), Access(), 45)));
  const auto history = resumed.Generation().input.items;
  ResponsesCompactionPlan compact;
  ChatPlanError error;
  Q4T_CHECK(PlanResponsesCompaction(history,
                                    ResponsesCompactionMode::kStandalone, 128,
                                    Config(), &compact, &error));
  const auto retained = OutputJson(compact.retained);
  Q4T_CHECK(retained.find(pending.GetArray("output")->array[0].GetString(
                "fingerprint")) != std::string::npos &&
            retained.find("value\\\":21") != std::string::npos);
  std::string wire;
  bool waiting;
  Q4T_CHECK(Ok(resumed.Begin(&wire)) &&
            Ok(resumed.RunPrograms(0, &waiting, Silent)) && !waiting &&
            Ok(resumed.Finish(false, 0, {}, 47, &wire)));
  Q4T_CHECK(resumed.PublicResponse().GetArray("output")->array[0].GetString(
                "result") == "42");
  return true;
}

Q4T_TEST(responses_program_api_media_is_prepared_reviewed_and_replayable) {
  Fixture f;
  ObjectStore objects(f.options);
  const std::string url =
      "data:image/png;base64,"
      "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGPYoiECAA"
      "KEAPFkHeRLAAAAAElFTkSuQmCC";
  auto config = Config();
  config.allow_media = true;
  auto request = Request();
  ResponsesExecution run(objects);
  std::string wire;
  bool waiting;
  Q4T_CHECK(Ok(run.Prepare(request, config, Access(), 42)) &&
            Ok(run.Begin(&wire)) &&
            run.Append(Code("text('actual pixels'); image('" + url + "');"), {},
                       &wire) &&
            Ok(run.RunPrograms(1, &waiting, Silent)) && !waiting);
  ModerationContent content;
  ChatPlanError error;
  Q4T_CHECK(CollectModerationOutput(*run.PublicResponse().GetArray("output"),
                                    &content, &error));
  Q4T_CHECK(content.images.array.size() == 1 && content.text &&
            OutputJson(content.records).find("fingerprint") ==
                std::string::npos);
  ResponsesGenerationPlan next;
  Q4T_CHECK(Ok(run.ContinueFileSearch(0, config, {}, &next)));
  std::string prompt;
  std::vector<VisionItem> media;
  Q4T_CHECK(PrepareChatPrompt(next.generation_request, {}, &prompt, &media,
                              &error.param, &error.code, nullptr, false,
                              next.input.async_tool_call_ids, true) &&
            media.size() == 1);
  Q4T_CHECK(Ok(run.Finish(false, 1, {}, 44, &wire)));
  const auto response = run.PublicResponse();
  Set(&request, "previous_response_id", *response.Find("id"));
  Set(&request, "input", String("Describe that image"));
  ResponsesExecution restored(objects);
  Q4T_CHECK(Ok(restored.Prepare(request, config, Access(), 45)) &&
            !restored.PendingProgramResume());
  Json frozen;
  Q4T_CHECK(Ok(restored.FreezeInputs(&frozen)));
  ResponsesExecution queued(objects);
  Q4T_CHECK(Ok(queued.RestoreInputs(frozen, config, Access(), 46)));
  Q4T_CHECK(queued.FileUsage() && queued.FileUsage()->images == 1);
  Q4T_CHECK(Ok(queued.Begin(&wire)) &&
            Ok(queued.Finish(false, 0, {}, 47, &wire)));
  return true;
}

Q4T_TEST(
    responses_program_api_media_budget_and_bad_pixels_fail_before_publish) {
  Fixture f;
  ObjectStore objects(f.options);
  for (const auto* source :
       {"data:image/png;base64,eA==", "file:///etc/passwd"}) {
    ResponsesExecution run(objects);
    auto config = Config();
    config.allow_media = true;
    Q4T_CHECK(Ok(run.Prepare(Request(), config, Access(), 42)));
    std::string wire;
    bool waiting;
    Q4T_CHECK(
        Ok(run.Begin(&wire)) &&
        run.Append(Code(std::string("image('") + source + "');"), {}, &wire));
    Q4T_CHECK(!run.RunPrograms(1, &waiting, Silent).ok() &&
              run.Output().Snapshot().GetArray("output")->array.empty());
    ResponsesExecution::ObserveError(&run, 502, "Invalid image",
                                     "program_output_invalid");
    Q4T_CHECK(Ok(run.CloseUnfinished()));
  }
  Json decoded;
  std::string encoded;
  const auto literal = String("q4t_program_content_v1\n[]");
  Q4T_CHECK(EncodeResponsesProgramOutput(literal, &encoded) &&
            DecodeResponsesProgramOutput(encoded, &decoded) &&
            decoded.array[0].GetString("text") == literal.str);
  Q4T_CHECK(
      !DecodeResponsesProgramOutput("q4t_program_content_v1\n{}", &decoded));
  return true;
}

Q4T_TEST(responses_program_api_async_children_allow_one_independent_answer) {
  Fixture f;
  ObjectStore objects(f.options);
  auto request = Request();
  Set(&Field(request, "tools").array[1], "async", Boolean(true));
  ResponsesExecution run(objects);
  std::string wire;
  bool waiting, answer;
  Q4T_CHECK(Ok(run.Prepare(request, Config(), Access(), 42)) &&
            Ok(run.Begin(&wire)) &&
            run.Append(
                Code("text((await tools.read({path:'docs/MODEL.md'})).value);"),
                {}, &wire) &&
            Ok(run.RunPrograms(1, &waiting, Silent)) && waiting);
  ResponsesGenerationPlan next;
  Q4T_CHECK(
      Ok(run.ContinueAsyncTools(1, 9, Config(), {}, &next, &answer, &wire)) &&
      answer);
  Q4T_CHECK(next.generation_request.GetString("tool_choice") == "none" &&
            next.generation_request.GetInt("max_completion_tokens") == 119);
  Q4T_CHECK(
      Ok(run.ContinueAsyncTools(1, 9, Config(), {}, &next, &answer, &wire)) &&
      !answer);
  Q4T_CHECK(run.Append(Text("The document lookup is pending."), {}, &wire) &&
            Ok(run.Finish(false, 1, {}, 44, &wire)));
  auto pending = run.PublicResponse();
  Q4T_CHECK(pending.GetArray("output")->array[1].GetBool("async"));
  Set(&request, "previous_response_id", *pending.Find("id"));
  Set(&request, "input", Array({Result(pending)}));
  ResponsesExecution resumed(objects);
  Q4T_CHECK(Ok(resumed.Prepare(request, Config(), Access(), 45)) &&
            Ok(resumed.Begin(&wire)) &&
            Ok(resumed.RunPrograms(0, &waiting, Silent)) && !waiting &&
            Ok(resumed.Finish(false, 0, {}, 46, &wire)));
  Q4T_CHECK(resumed.PublicResponse().GetArray("output")->array[0].GetString(
                "result") == "21");
  // A synchronous direct sibling still blocks an independent answer.
  auto sync = Field(request, "tools").array[1];
  Set(&sync, "name", String("read_sync"));
  Set(&sync, "async", Boolean(false));
  Set(&sync, "allowed_callers", Parse(R"(["direct"])"));
  Field(request, "tools").array.push_back(sync);
  std::erase_if(request.object, [](const auto& field) {
    return field.first == "previous_response_id";
  });
  Set(&request, "input", String("parallel work"));
  ResponsesExecution mixed(objects);
  Q4T_CHECK(Ok(mixed.Prepare(request, Config(), Access(), 47)) &&
            Ok(mixed.Begin(&wire)));
  auto direct = Code("unused", 0);
  direct.call.name = "read_sync";
  direct.call.arguments = "{\"path\":\"direct\"}";
  Q4T_CHECK(mixed.Append(direct, {}, &wire));
  Q4T_CHECK(
      mixed.Append(Code("await tools.read({path:'nested'});", 1), {}, &wire) &&
      Ok(mixed.RunPrograms(2, &waiting, Silent)) && waiting);
  Q4T_CHECK(
      Ok(mixed.ContinueAsyncTools(2, 7, Config(), {}, &next, &answer, &wire)) &&
      !answer);
  Q4T_CHECK(Ok(mixed.Finish(false, 2, {}, 48, &wire)));
  return true;
}

Q4T_TEST(responses_program_api_sync_child_blocks_direct_async_answer) {
  Fixture f;
  ObjectStore objects(f.options);
  auto request = Request();
  auto direct_tool = Field(request, "tools").array[1];
  Set(&direct_tool, "name", String("read_async"));
  Set(&direct_tool, "async", Boolean(true));
  Set(&direct_tool, "allowed_callers", Parse(R"(["direct"])"));
  Field(request, "tools").array.push_back(direct_tool);
  ResponsesExecution run(objects);
  std::string wire;
  Q4T_CHECK(Ok(run.Prepare(request, Config(), Access(), 42)) &&
            Ok(run.Begin(&wire)));
  auto direct = Code("unused", 0);
  direct.call.name = "read_async";
  direct.call.arguments = "{\"path\":\"direct\"}";
  Q4T_CHECK(
      run.Append(direct, {}, &wire) &&
      run.Append(Code("await tools.read({path:'nested'});", 1), {}, &wire));
  bool waiting, answer;
  Q4T_CHECK(Ok(run.RunPrograms(2, &waiting, Silent)) && waiting &&
            run.HasAsyncTools());
  const auto before = OutputJson(run.Output().Snapshot());
  ResponsesGenerationPlan next;
  Q4T_CHECK(
      Ok(run.ContinueAsyncTools(2, 9, Config(), {}, &next, &answer, &wire)) &&
      !answer && wire.empty());
  Q4T_CHECK(OutputJson(run.Output().Snapshot()) == before);
  Q4T_CHECK(Ok(run.Finish(false, 2, {}, 44, &wire)));
  return true;
}

Q4T_TEST(
    responses_program_api_discovery_loads_namespace_authority_at_position) {
  Fixture f;
  ObjectStore objects(f.options);
  auto request = Request();
  auto tool = Field(request, "tools").array[1];
  Set(&tool, "defer_loading", Boolean(true));
  const auto namespaced = Object({{"type", String("namespace")},
                                  {"name", String("docs")},
                                  {"description", String("documents")},
                                  {"tools", Array({tool})}});
  Field(request, "tools").array[1] = namespaced;
  Field(request, "tools")
      .array.push_back(Parse(R"({"type":"tool_search","execution":"server"})"));
  ResponsesExecution hidden(objects);
  Q4T_CHECK(Ok(hidden.Prepare(request, Config(), Access(), 42)));
  const auto before = OutputJson(hidden.Generation().generation_request);
  Q4T_CHECK(before.find("Authorized definitions: []") != std::string::npos);
  Set(&request, "input",
      Array({Object({{"role", String("user")},
                     {"content", String("read project")}}),
             Object({{"type", String("additional_tools")},
                     {"role", String("developer")},
                     {"tools", Array({namespaced})}})}));
  auto pending =
      Pending(objects, request,
              "text((await tools.docs.read({path:'docs/MODEL.md'})).value);");
  Q4T_CHECK(pending.GetArray("output")->array[1].GetString("namespace") ==
            "docs");
  Set(&request, "previous_response_id", *pending.Find("id"));
  Set(&request, "input", Array({Result(pending)}));
  ResponsesExecution resumed(objects);
  std::string wire;
  bool waiting;
  Q4T_CHECK(Ok(resumed.Prepare(request, Config(), Access(), 45)) &&
            Ok(resumed.Begin(&wire)) &&
            Ok(resumed.RunPrograms(0, &waiting, Silent)) && !waiting &&
            Ok(resumed.Finish(false, 0, {}, 46, &wire)));
  Q4T_CHECK(resumed.PublicResponse().GetArray("output")->array[0].GetString(
                "result") == "21");
  return true;
}

Q4T_TEST(responses_program_api_count_authenticates_without_running_program) {
  Fixture f;
  ObjectStore objects(f.options);
  const auto pending = Pending(objects);
  auto request = Request();
  std::erase_if(request.object, [](const auto& field) {
    return field.first == "max_output_tokens";
  });
  Set(&request, "previous_response_id", *pending.Find("id"));
  Set(&request, "input", Array());
  auto config = Config();
  config.count_input_only = true;
  ResponsesEnvelope envelope;
  ResponsesGenerationPlan generation;
  Q4T_CHECK(Ok(PrepareResponsesTokenCount(request, config, objects, Access(),
                                          &envelope, &generation)));
  Q4T_CHECK(generation.input.items.array.size() == 3 &&
            generation.input.items.array.back().GetString("type") ==
                "function_call");
  std::erase_if(request.object, [](const auto& field) {
    return field.first == "previous_response_id";
  });
  Set(&request, "input", *pending.GetArray("output"));
  Set(&Field(request, "input").array[0], "code", String("text('changed')"));
  Q4T_CHECK(PrepareResponsesTokenCount(request, config, objects, Access(),
                                       &envelope, &generation)
                .http_status == 400);
  return true;
}

Q4T_TEST(responses_program_api_media_quota_cannot_refill_between_programs) {
  Fixture f;
  ObjectStore objects(f.options);
  auto config = Config();
  config.allow_media = true;
  FileExpansionOptions limits;
  limits.max_files = 1;
  config.file_inputs = std::make_shared<FileExpansionOptions>(limits);
  const std::string code =
      "image('data:image/png;base64,"
      "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGPYoiECAA"
      "KEAPFkHeRLAAAAAElFTkSuQmCC');";
  ResponsesExecution run(objects);
  std::string wire;
  bool waiting;
  Q4T_CHECK(Ok(run.Prepare(Request(), config, Access(), 42)) &&
            Ok(run.Begin(&wire)) && run.Append(Code(code), {}, &wire) &&
            Ok(run.RunPrograms(1, &waiting, Silent)) && !waiting);
  const auto first = OutputJson(run.Output().Snapshot());
  Q4T_CHECK(run.Append(Code(code), {}, &wire));
  const auto failure = run.RunPrograms(1, &waiting, Silent);
  Q4T_CHECK(failure.http_status == 413 &&
            failure.code == "file_input_capacity_exceeded" &&
            OutputJson(run.Output().Snapshot()) == first);
  ResponsesExecution::ObserveError(&run, 413, "Media budget", failure.code);
  Q4T_CHECK(Ok(run.CloseUnfinished()));
  return true;
}
