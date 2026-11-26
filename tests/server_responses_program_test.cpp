#include "q4t/server/responses_program.h"
#include "q4t/server/chat_protocol_identity.h"
#include "q4t/server/responses_output.h"

#include <cstdlib>
#include <filesystem>
#include <stdexcept>

#include "../src/server/object_protocol.h"
#include "q4t/test.h"

namespace {
using namespace q4t::server;
using namespace q4t::server::object_detail;
using namespace std::chrono_literals;
using Clock = std::chrono::steady_clock;
Json Parse(const std::string& text) {
  Json value;
  if (!q4t::io::ParseJson(text, &value, true))
    throw std::runtime_error("fixture");
  return value;
}
StateAccess Access(std::string scope = "client:program") {
  return {std::move(scope), Clock::now() + 8s, {}};
}
ChatPlanConfig Config() {
  ChatPlanConfig config;
  config.model = "program-model";
  return config;
}
Json Tools() {
  return Parse(R"([
    {"type":"programmatic_tool_calling"},
    {"type":"function","name":"read","allowed_callers":["programmatic"],
     "parameters":{"type":"object","properties":{"n":{"type":"integer"}},
                   "required":["n"],"additionalProperties":false},
     "output_schema":{"type":"object","properties":{"value":{"type":"integer"}},
                      "required":["value"],"additionalProperties":false}},
    {"type":"function","name":"direct","parameters":{"type":"object"}}
  ])");
}
bool Ok(const ObjectReply& reply) {
  if (!reply.ok())
    std::fprintf(stderr, "%s: %s\n", reply.code.c_str(), reply.message.c_str());
  return reply.ok();
}
struct Fixture {
  ObjectStoreOptions options;
  Fixture() {
    const char* output = std::getenv("Q4T_TEST_OUTPUT_DIR");
    auto path = std::filesystem::absolute(
        output ? output : ".q4t-work/responses-program-cpu");
    std::filesystem::create_directories(path);
    auto name = (path / "program-XXXXXX").string();
    if (!mkdtemp(name.data())) throw std::runtime_error("fixture path");
    options.directory = name + "/objects";
    options.restart_backoff_ms = 0;
  }
};
Json Create(StateCipher& cipher, std::string code, Json tools = Tools()) {
  Json program;
  const auto result = CreateResponsesProgram(cipher, std::move(code), tools,
                                             Config(), Access(), &program);
  if (!Ok(result)) throw std::runtime_error("create program");
  return program;
}
Json Result(const Json& call, Json value) {
  return Object({{"type", String(call.GetString("type") == "function_call"
                                     ? "function_call_output"
                                     : "custom_tool_call_output")},
                 {"call_id", *call.Find("call_id")},
                 {"caller", *call.Find("caller")},
                 {"output", std::move(value)}});
}
ObjectReply Run(StateCipher& cipher, const Json& history, Json* output) {
  std::vector<ResponsesProgramState> states;
  auto result =
      OpenResponsesPrograms(cipher, history, Config(), Access(), &states);
  if (!result.ok()) return result;
  if (states.size() != 1) throw std::runtime_error("program count");
  return AdvanceResponsesProgram(states.front(), Access(), output);
}
}  // namespace

Q4T_TEST(responses_program_callers_are_exact_and_default_direct) {
  auto tool = Object();
  Q4T_CHECK(ResponsesToolAllows(tool, "direct") &&
            !ResponsesToolAllows(tool, "programmatic"));
  for (const auto* text :
       {R"(["direct"])", R"(["programmatic"])", R"(["direct","programmatic"])",
        R"(["programmatic","direct"])", "null"}) {
    Set(&tool, "allowed_callers", Parse(text));
    ChatPlanError error;
    Q4T_CHECK(ValidateResponsesAllowedCallers(tool, &error, "tools[0]"));
  }
  for (const auto* text : {"[]", R"(["direct","direct"])", R"(["program"])",
                           R"("programmatic")", R"([null])"}) {
    Set(&tool, "allowed_callers", Parse(text));
    ChatPlanError error;
    Q4T_CHECK(!ValidateResponsesAllowedCallers(tool, &error, "tools[0]") &&
              error.param == "tools[0].allowed_callers" &&
              !ResponsesToolAllows(tool, "direct") &&
              !ResponsesToolAllows(tool, "programmatic"));
  }
  return true;
}

Q4T_TEST(responses_program_authenticated_restart_and_real_results) {
  Fixture f;
  Json history, calls;
  {
    ObjectStore store(f.options);
    StateCipher cipher(store);
    history = Array({Create(
        cipher, "const a = await tools.read({n:7}); text(a.value * 2);")});
    Q4T_CHECK(Ok(Run(cipher, history, &calls)) && calls.array.size() == 1);
    Q4T_CHECK(calls.array[0].GetString("type") == "function_call" &&
              calls.array[0].Find("caller")->GetString("caller_id") ==
                  history.array[0].GetString("call_id"));
  }
  history.array.push_back(calls.array[0]);
  history.array.push_back(Result(calls.array[0], String("{\"value\":21}")));
  ObjectStore restarted(f.options);
  StateCipher cipher(restarted);
  Json completed;
  Q4T_CHECK(Ok(Run(cipher, history, &completed)) &&
            completed.array.size() == 1);
  Q4T_CHECK(completed.array[0].GetString("type") == "program_output" &&
            completed.array[0].GetString("result") == "42" &&
            completed.array[0].GetString("status") == "completed");
  history.array.push_back(completed.array[0]);
  Json replay;
  Q4T_CHECK(Ok(Run(cipher, history, &replay)) && replay.array.empty());
  return true;
}

Q4T_TEST(responses_program_fingerprint_rejects_scope_model_and_code_changes) {
  Fixture f;
  ObjectStore store(f.options);
  StateCipher cipher(store);
  const auto program = Create(cipher, "text('authenticated');");
  std::vector<ResponsesProgramState> states;
  for (const auto* field : {"code", "id", "call_id", "fingerprint"}) {
    auto modified = program;
    Set(&modified, field, String(program.GetString(field) + "a"));
    Q4T_CHECK(!OpenResponsesPrograms(cipher, Array({modified}), Config(),
                                     Access(), &states)
                   .ok());
  }
  auto config = Config();
  config.model = "other-model";
  Q4T_CHECK(!OpenResponsesPrograms(cipher, Array({program}), config, Access(),
                                   &states)
                 .ok());
  Q4T_CHECK(!OpenResponsesPrograms(cipher, Array({program}), Config(),
                                   Access("client:other"), &states)
                 .ok());
  Q4T_CHECK(Ok(OpenResponsesPrograms(cipher, Array({program}), Config(),
                                     Access(), &states)));
  return true;
}

Q4T_TEST(responses_program_direct_only_and_unloaded_tools_do_not_execute) {
  Fixture f;
  ObjectStore store(f.options);
  StateCipher cipher(store);
  for (const auto* code :
       {"text(await tools.direct({}));", "text(await tools.not_loaded({}));"}) {
    Json output;
    Q4T_CHECK(Ok(Run(cipher, Array({Create(cipher, code)}), &output)));
    Q4T_CHECK(output.array.size() == 1 &&
              output.array[0].GetString("type") == "program_output" &&
              output.array[0].GetString("status") == "incomplete");
  }
  Json program;
  auto tools = Tools();
  tools.array.erase(tools.array.begin());
  Q4T_CHECK(!CreateResponsesProgram(cipher, "text(1)", tools, Config(),
                                    Access(), &program)
                 .ok());
  return true;
}

Q4T_TEST(responses_program_argument_and_original_result_schema_enforced) {
  Fixture f;
  ObjectStore store(f.options);
  StateCipher cipher(store);
  Json output;
  for (const auto* code : {"await tools.read({n:'wrong'});",
                           "await tools.read({n:1,extra:true});"}) {
    const auto invalid = Run(cipher, Array({Create(cipher, code)}), &output);
    Q4T_CHECK(invalid.http_status == 502 &&
              invalid.code == "program_arguments_invalid");
    Q4T_CHECK(output.array.empty());
  }
  auto history = Array({Create(cipher, "text(await tools.read({n:1}));")});
  Q4T_CHECK(Ok(Run(cipher, history, &output)) && output.array.size() == 1);
  history.array.push_back(output.array[0]);
  history.array.push_back(
      Result(output.array[0], String("{\"value\":\"bad\"}")));
  const auto wrong = Run(cipher, history, &output);
  Q4T_CHECK(wrong.http_status == 400 && wrong.code == "invalid_tool_output");
  Set(&history.array.back(), "output", String("{\"value\":17}"));
  Q4T_CHECK(Ok(Run(cipher, history, &output)) &&
            output.array[0].GetString("result") == "{\"value\":17}");
  return true;
}

Q4T_TEST(responses_program_result_caller_and_identity_cannot_be_rebound) {
  Fixture f;
  ObjectStore store(f.options);
  StateCipher cipher(store);
  auto history = Array({Create(cipher, "text(await tools.read({n:1}));")});
  Json calls;
  Q4T_CHECK(Ok(Run(cipher, history, &calls)));
  history.array.push_back(calls.array[0]);
  history.array.push_back(Result(calls.array[0], String("{\"value\":1}")));
  for (int variation = 0; variation < 6; ++variation) {
    auto changed = history;
    auto& result = changed.array.back();
    if (variation == 0)
      Set(&result, "caller", Object({{"type", String("direct")}}));
    if (variation == 1)
      Set(&Field(result, "caller"), "caller_id", String("other"));
    if (variation == 2) Set(&result, "name", String("other"));
    if (variation == 3) Set(&result, "type", String("custom_tool_call_output"));
    if (variation == 4) changed.array.push_back(result);
    if (variation == 5) Set(&changed.array[1], "call_id", String("forged"));
    std::vector<ResponsesProgramState> states;
    Q4T_CHECK(
        !OpenResponsesPrograms(cipher, changed, Config(), Access(), &states)
             .ok());
  }
  return true;
}

Q4T_TEST(responses_program_causal_replay_rejects_forged_arguments_and_order) {
  Fixture f;
  ObjectStore store(f.options);
  StateCipher cipher(store);
  auto history = Array({Create(cipher,
                               "const a = await tools.read({n:1}); text(await "
                               "tools.read({n:a.value}));")});
  Json output;
  Q4T_CHECK(Ok(Run(cipher, history, &output)) && output.array.size() == 1);
  history.array.push_back(output.array[0]);
  auto changed = history;
  Set(&changed.array[1], "arguments", String("{\"n\":2}"));
  Q4T_CHECK(Run(cipher, changed, &output).code == "invalid_program_state");
  history.array.push_back(Result(history.array[1], String("{\"value\":9}")));
  Q4T_CHECK(Ok(Run(cipher, history, &output)) && output.array.size() == 1 &&
            output.array[0].GetString("arguments") == "{\"n\":9}");
  history.array.push_back(output.array[0]);
  changed = history;
  std::swap(changed.array[2], changed.array[3]);
  Q4T_CHECK(Run(cipher, changed, &output).code == "invalid_program_state");
  history.array.push_back(Result(history.array[3], String("{\"value\":42}")));
  Q4T_CHECK(Ok(Run(cipher, history, &output)) && output.array.size() == 1 &&
            output.array[0].GetString("result") == "{\"value\":42}");
  return true;
}

Q4T_TEST(responses_program_namespace_custom_format_dispatch_checked) {
  Fixture f;
  ObjectStore store(f.options);
  StateCipher cipher(store);
  {
    auto tools = Parse(R"([{"type":"programmatic_tool_calling"},
      {"type":"custom","name":"read","allowed_callers":["programmatic"]}])");
    Json output;
    Q4T_CHECK(Ok(Run(cipher,
                     Array({Create(cipher, "text(await tools.read('free'));",
                                   tools)}),
                     &output)) &&
              output.array.size() == 1 &&
              output.array[0].GetString("input") == "free");
  }
  for (const auto* syntax : {"regex", "lark"}) {
    auto tools = Parse(R"([{"type":"programmatic_tool_calling"},
      {"type":"namespace","name":"docs","description":"documents","tools":[
        {"type":"custom","name":"read","allowed_callers":["programmatic"],
         "format":{"type":"grammar","syntax":"regex","definition":"DOC[0-9]+"}}
      ]}])");
    auto& format = Field(Field(tools.array[1], "tools").array[0], "format");
    Set(&format, "syntax", String(syntax));
    if (std::string(syntax) == "lark")
      Set(&format, "definition", String("start: /DOC[0-9]+/"));
    Json output;
    auto history =
        Array({Create(cipher, "text(await tools.docs.read('DOC42'));", tools)});
    const auto dispatched = Run(cipher, history, &output);
#ifndef Q4T_ENABLE_XGRAMMAR
    // The public CPU build has no grammar validator. It must fail closed
    // before dispatch, including when a program invokes a custom tool.
    Q4T_CHECK(dispatched.http_status == 503 &&
              dispatched.code == "tool_result_validation_unavailable" &&
              output.array.empty());
#else
    Q4T_CHECK(Ok(dispatched) && output.array.size() == 1);
    Q4T_CHECK(output.array[0].GetString("namespace") == "docs" &&
              output.array[0].GetString("input") == "DOC42");
    history.array.push_back(output.array[0]);
    history.array.push_back(Result(output.array[0], String("actual document")));
    // The durable Item view adds lifecycle metadata to both custom items.
    Set(&history.array.back(), "status", String("completed"));
    auto malformed = history;
    Set(&malformed.array[1], "status", String("unknown"));
    Q4T_CHECK(Run(cipher, malformed, &output).http_status == 400);
    Q4T_CHECK(Ok(Run(cipher, history, &output)) &&
              output.array[0].GetString("result") == "actual document");
    auto bad =
        Array({Create(cipher, "text(await tools.docs.read('WRONG'));", tools)});
    const auto rejected = Run(cipher, bad, &output);
    Q4T_CHECK(rejected.http_status == 502 &&
              rejected.code == "program_arguments_invalid" &&
              output.array.empty());
#endif
  }
  return true;
}

Q4T_TEST(responses_program_cancel_and_expiry_do_not_dispatch) {
  Fixture f;
  ObjectStore store(f.options);
  StateCipher cipher(store);
  const auto program = Create(cipher, "text(await tools.read({n:1}));");
  std::vector<ResponsesProgramState> states;
  Q4T_CHECK(Ok(OpenResponsesPrograms(cipher, Array({program}), Config(),
                                     Access(), &states)));
  auto cancelled = Access();
  cancelled.cancelled = [] { return true; };
  Json emitted;
  Q4T_CHECK(
      AdvanceResponsesProgram(states[0], cancelled, &emitted).http_status ==
          499 &&
      emitted.array.empty());
  Set(&states[0].worker_input, "expires_at", Number(1));
  Q4T_CHECK(AdvanceResponsesProgram(states[0], Access(), &emitted).code ==
                "invalid_program_state" &&
            emitted.array.empty());
  Json healthy;
  Q4T_CHECK(
      Ok(Run(cipher, Array({Create(cipher, "text('healthy');")}), &healthy)) &&
      healthy.array[0].GetString("result") == "healthy");
  return true;
}

Q4T_TEST(responses_program_patch_preserves_client_failure_after_reopen) {
  Fixture f;
  const auto tools = Parse(R"([{"type":"programmatic_tool_calling"},
      {"type":"apply_patch","allowed_callers":["programmatic"]}])");
  Json history, calls, completed;
  {
    ObjectStore store(f.options);
    StateCipher cipher(store);
    const auto invalid = Array({Create(
        cipher,
        "await tools.apply_patch({type:'update_file',path:'draft.md'});",
        tools)});
    Q4T_CHECK(!Run(cipher, invalid, &calls).ok() && calls.array.empty());
    history = Array({Create(
        cipher,
        "const r=await tools.apply_patch({type:'delete_file',path:'draft.md'});"
        "text(r.status+' '+r.output);",
        tools)});
    Q4T_CHECK(Ok(Run(cipher, history, &calls)) && calls.array.size() == 1);
  }
  const auto& call = calls.array[0];
  Q4T_CHECK(call.GetString("type") == "apply_patch_call" &&
            call.Find("operation")->GetString("path") == "draft.md" &&
            call.Find("caller")->GetString("caller_id") ==
                history.array[0].GetString("call_id"));
  history.array.push_back(call);
  history.array.push_back(Object({{"type", String("apply_patch_call_output")},
                                  {"call_id", *call.Find("call_id")},
                                  {"caller", *call.Find("caller")},
                                  {"status", String("failed")},
                                  {"output", String("file absent")}}));
  ObjectStore store(f.options);
  StateCipher cipher(store);
  Q4T_CHECK(Ok(Run(cipher, history, &completed)) &&
            completed.array.size() == 1);
  Q4T_CHECK(completed.array.front().GetString("result") ==
            "failed file absent");
  Set(&Field(history.array[1], "operation"), "path", String("rebound.md"));
  Q4T_CHECK(!Run(cipher, history, &completed).ok());
  return true;
}

Q4T_TEST(responses_program_shell_reopens_with_canonical_commands_and_outcomes) {
  Fixture f;
  const auto tools = Parse(R"([{"type":"programmatic_tool_calling"},
    {"type":"shell","environment":{"type":"local"},"allowed_callers":["programmatic"]}])");
  Json history, calls, completed;
  {
    ObjectStore store(f.options);
    StateCipher cipher(store);
    auto invalid =
        Array({Create(cipher, "await tools.shell({commands:[2]});", tools)});
    Q4T_CHECK(!Run(cipher, invalid, &calls).ok() && calls.array.empty());
    history = Array(
        {Create(cipher,
                "const r=await "
                "tools.shell({max_output_length:80,commands:['pwd','date']});"
                "text([r.status,r.output[0].stdout,r.output[1].outcome.type,r."
                "max_output_length].join('|'));",
                tools)});
    Q4T_CHECK(Ok(Run(cipher, history, &calls)) && calls.array.size() == 1);
  }
  const auto child = calls.array.front();
  Q4T_CHECK(child.GetString("type") == "shell_call" &&
            child.Find("action")->GetArray("commands")->array.size() == 2 &&
            child.Find("action")->Find("timeout_ms")->IsNull() &&
            child.Find("environment")->GetString("type") == "local" &&
            child.Find("caller")->GetString("caller_id") ==
                history.array[0].GetString("call_id"));
  ResponsesOutput published;
  std::string wire;
  Q4T_CHECK(published.Init(Object({{"tools", tools}}), "resp_program_shell", 42,
                           false) &&
            published.Start(&wire) &&
            published.AppendProgramItems(calls, &wire));
  Q4T_CHECK(wire.find("response.shell_call_command.done") != wire.npos &&
            wire.find("response.function_call_arguments") == wire.npos &&
            published.Snapshot()
                    .GetArray("output")
                    ->array.front()
                    .Find("caller")
                    ->GetString("caller_id") ==
                history.array.front().GetString("call_id"));
  history.array.push_back(child);
  auto result = Parse(R"({"type":"shell_call_output","max_output_length":80,
    "status":"incomplete","output":[{"stdout":"project","stderr":"",
    "outcome":{"type":"exit","exit_code":0}},{"stdout":"","stderr":"",
    "outcome":{"type":"timeout"}}]})");
  Set(&result, "caller", *child.Find("caller"));
  Set(&result, "call_id", *child.Find("call_id"));
  history.array.push_back(result);
  ObjectStore store(f.options);
  StateCipher cipher(store);
  Q4T_CHECK(Ok(Run(cipher, history, &completed)) &&
            completed.array.size() == 1 &&
            completed.array[0].GetString("result") ==
                "incomplete|project|timeout|80");
  auto wrong = history;
  Set(&Field(wrong.array.back(), "caller"), "caller_id", String("other"));
  Q4T_CHECK(!Run(cipher, wrong, &completed).ok());
  Set(&history.array.back(), "status", String("in_progress"));
  Q4T_CHECK(!Run(cipher, history, &completed).ok());
  return true;
}

Q4T_TEST(responses_program_forks_keep_independent_terminal_item_ids) {
  Fixture f;
  ObjectStore store(f.options);
  StateCipher cipher(store);
  auto history =
      Array({Create(cipher, "const r=await tools.read({n:1});text(r.value);")});
  Json calls;
  Q4T_CHECK(Ok(Run(cipher, history, &calls)) && calls.array.size() == 1);
  history.array.push_back(calls.array.front());
  history.array.push_back(
      Result(calls.array.front(), String(R"({"value":1})")));
  Json first, retry, second;
  Q4T_CHECK(Ok(Run(cipher, history, &first)) && first.array.size() == 1 &&
            first.array[0].GetString("result") == "1");
  Q4T_CHECK(Ok(Run(cipher, history, &retry)) &&
            OutputJson(first) == OutputJson(retry));
  Set(&history.array.back(), "output", String(R"({"value":2})"));
  Q4T_CHECK(Ok(Run(cipher, history, &second)) && second.array.size() == 1 &&
            second.array[0].GetString("result") == "2");
  Q4T_CHECK(second.array[0].GetString("call_id") ==
                first.array[0].GetString("call_id") &&
            second.array[0].GetString("id") != first.array[0].GetString("id"));
  history.array.push_back(second.array.front());
  // Previously issued terminal IDs remain readable after upgrade.
  Set(&history.array.back(), "id",
      String("prog_out_" +
             Digest(history.array[0].GetString("call_id")).substr(0, 56)));
  return Ok(Run(cipher, history, &retry)) && retry.array.empty();
}

Q4T_TEST(responses_program_child_identity_tracks_results_consumed_by_the_vm) {
  Fixture f;
  ObjectStore store(f.options);
  StateCipher cipher(store);
  for (const bool same_args : {false, true}) {
    const std::string arg = same_args ? "2" : "a.value";
    auto history = Array({Create(
        cipher, "const a=await tools.read({n:1});const b=await tools.read({n:" +
                    arg + "});text(b.value);")});
    Json calls, first, second, retry;
    Q4T_CHECK(Ok(Run(cipher, history, &calls)) && calls.array.size() == 1);
    history.array.push_back(calls.array.front());
    history.array.push_back(
        Result(calls.array.front(), String(R"({"value":2})")));
    Q4T_CHECK(Ok(Run(cipher, history, &first)) && first.array.size() == 1);
    Q4T_CHECK(Ok(Run(cipher, history, &retry)) &&
              OutputJson(retry) == OutputJson(first));
    Set(&history.array.back(), "output", String(R"({"value":3})"));
    Q4T_CHECK(Ok(Run(cipher, history, &second)) && second.array.size() == 1);
    const auto& a = first.array[0];
    const auto& b = second.array[0];
    Q4T_CHECK(a.GetString("arguments") == R"({"n":2})" &&
              b.GetString("arguments") ==
                  (same_args ? R"({"n":2})" : R"({"n":3})"));
    Q4T_CHECK(a.GetString("id") != b.GetString("id") &&
              a.GetString("call_id") != b.GetString("call_id"));
    history.array.push_back(a);
    Q4T_CHECK(!Run(cipher, history, &retry).ok());
    history.array.back() = b;
    history.array.push_back(Result(b, String(R"({"value":8})")));
    Q4T_CHECK(Ok(Run(cipher, history, &retry)) &&
              retry.array[0].GetString("result") == "8");
  }
  return true;
}

Q4T_TEST(
    responses_program_legacy_dependent_call_reopens_without_downgrading_new_parents) {
  Fixture f;
  ObjectStore store(f.options);
  StateCipher cipher(store);
  auto parent = Create(cipher,
                       "const a=await tools.read({n:1});const b=await "
                       "tools.read({n:a.value});text(b.value);");
  auto original = parent;
  const StateCipherBinding binding{
      Config().model,
      Digest(std::string(identity::kTemplateReference) + ':' +
             identity::kRenderer),
      "responses.program.v1", parent.GetString("id")};
  std::string plain, sealed;
  Q4T_CHECK(Ok(
      cipher.Open(parent.GetString("fingerprint"), binding, Access(), &plain)));
  auto payload = Parse(plain);
  Q4T_CHECK(std::erase_if(payload.object, [](const auto& field) {
              return field.first == "result_bound_calls";
            }) == 1);
  Q4T_CHECK(Ok(cipher.Seal(OutputJson(payload), binding, Access(), &sealed)));
  Set(&parent, "fingerprint", String(sealed));
  auto history = Array({parent});
  Json result;
  Q4T_CHECK(Ok(Run(cipher, history, &result)) && result.array.size() == 1);
  history.array.push_back(result.array[0]);
  history.array.push_back(Result(result.array[0], String(R"({"value":2})")));
  Q4T_CHECK(Ok(Run(cipher, history, &result)) && result.array.size() == 1);
  auto legacy = result.array[0];
  const auto old_id =
      "call_p_" + Digest(parent.GetString("call_id") + ":1").substr(0, 56);
  Q4T_CHECK(old_id != legacy.GetString("call_id"));
  Set(&legacy, "call_id", String(old_id));
  Set(&legacy, "id", String("fc_" + Digest(old_id).substr(0, 56)));
  history.array.push_back(legacy);
  history.array.push_back(Result(legacy, String(R"({"value":9})")));
  Q4T_CHECK(Ok(Run(cipher, history, &result)) &&
            result.array[0].GetString("result") == "9");
  history.array[0] = original;
  return !Run(cipher, history, &result).ok();
}

Q4T_TEST(responses_program_child_identity_preserves_vm_object_key_order) {
  Fixture f;
  ObjectStore store(f.options);
  StateCipher cipher(store);
  auto tools = Tools();
  auto& definition = tools.array[1];
  std::erase_if(definition.object, [](const auto& field) {
    return field.first == "output_schema";
  });
  auto history =
      Array({Create(cipher,
                    "const a=await tools.read({n:1});"
                    "await tools.read({n:Object.keys(a)[0]==='left'?2:3});",
                    tools)});
  Json initial, left, right;
  Q4T_CHECK(Ok(Run(cipher, history, &initial)) && initial.array.size() == 1);
  history.array.push_back(initial.array.front());
  history.array.push_back(
      Result(initial.array.front(), String(R"({"left":0,"right":0})")));
  Q4T_CHECK(Ok(Run(cipher, history, &left)) && left.array.size() == 1);
  Set(&history.array.back(), "output", String(R"({"right":0,"left":0})"));
  Q4T_CHECK(Ok(Run(cipher, history, &right)) && right.array.size() == 1);
  Q4T_CHECK(left.array[0].GetString("arguments") == R"({"n":2})" &&
            right.array[0].GetString("arguments") == R"({"n":3})");
  return left.array[0].GetString("call_id") !=
         right.array[0].GetString("call_id");
}
