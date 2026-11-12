#include "q4t/server/responses_program.h"

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
    Q4T_CHECK(Ok(Run(cipher, history, &output)) && output.array.size() == 1);
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
