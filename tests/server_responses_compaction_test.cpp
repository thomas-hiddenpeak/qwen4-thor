#include "q4t/server/responses_compaction.h"

#include <unistd.h>

#include <algorithm>
#include <cstdlib>
#include <filesystem>
#include <fstream>

#include "q4t/server/output_schema.h"
#include "q4t/server/application_routes.h"
#include "q4t/server/responses_api.h"
#include "q4t/server/responses_crypto.h"
#include "q4t/server/responses_execution.h"
#include "q4t/server/responses_reasoning.h"
#include "q4t/test.h"

namespace {
using namespace q4t::server;
using q4t::io::Json;
using Clock = std::chrono::steady_clock;
using namespace std::chrono_literals;
Json Parse(const std::string& text) {
  Json value;
  if (!q4t::io::ParseJson(text, &value, true))
    throw std::runtime_error("compaction fixture JSON");
  return value;
}
Json& Field(Json* value, const char* key) {
  for (auto& [name, item] : value->object)
    if (name == key) return item;
  value->object.emplace_back(key, Json());
  return value->object.back().second;
}
void String(Json* value, const char* key, std::string text) {
  auto& item = Field(value, key);
  item = {};
  item.type = Json::Type::kString;
  item.str = std::move(text);
}
Json Message(const char* id, const char* role, const char* text) {
  auto item = Parse(R"({"type":"message","status":"completed"})");
  String(&item, "id", id);
  String(&item, "role", role);
  auto content = Parse(R"([{"type":"input_text","text":""}])");
  String(&content.array[0], "text", text);
  if (std::string_view(role) == "assistant") {
    String(&content.array[0], "type", "output_text");
    Field(&content.array[0], "annotations") = Parse("[]");
  }
  Field(&item, "content") = std::move(content);
  return item;
}
Json History() {
  auto value = Parse("[]");
  value.array = {Message("sys", "system", "Do not change project files"),
                 Message("u1", "user", "Remember the result 42"),
                 Message("a1", "assistant", "The verified result is 42"),
                 Message("u2", "user", "What was the result?")};
  return value;
}
ChatPlanConfig Config() {
  ChatPlanConfig config;
  config.model = "cpu-model";
  return config;
}
StateAccess Access(std::string scope = "client:compact") {
  return {std::move(scope), Clock::now() + 5s, {}};
}
struct Fixture {
  ObjectStoreOptions options;
  Fixture() {
    const char* output = std::getenv("Q4T_TEST_OUTPUT_DIR");
    const auto root =
        std::filesystem::absolute(output ? output : ".q4t-work/compaction-cpu");
    std::filesystem::create_directories(root);
    auto path = (root / "compact-XXXXXX").string();
    if (!mkdtemp(path.data())) throw std::runtime_error("compaction directory");
    options.directory = path + "/objects";
    options.restart_backoff_ms = 0;
  }
};
ResponsesCompactionPlan Plan(
    const Json& history = History(),
    ResponsesCompactionMode mode = ResponsesCompactionMode::kAutomatic) {
  ResponsesCompactionPlan plan;
  ChatPlanError error;
  const auto result =
      PlanResponsesCompaction(history, mode, 256, Config(), &plan, &error);
  if (!result) throw std::runtime_error(error.param + ": " + result.message());
  return plan;
}
bool Ok(const ObjectReply& reply) {
  if (!reply.ok())
    std::fprintf(stderr, "%s: %s\n", reply.code.c_str(), reply.message.c_str());
  return reply.ok();
}
Json Call(const char* id, const char* call) {
  auto item = Parse(R"({"type":"function_call","name":"read_record",
    "arguments":"{}","status":"completed"})");
  String(&item, "id", id);
  String(&item, "call_id", call);
  return item;
}
Json Output(const char* id, const char* call) {
  auto item = Parse(R"({"type":"function_call_output","output":"42",
    "status":"completed"})");
  String(&item, "id", id);
  String(&item, "call_id", call);
  return item;
}
}  // namespace

Q4T_TEST(compaction_plan_preserves_instructions_and_current_turn) {
  const auto history = History();
  const auto plan = Plan(history);
  Q4T_CHECK(plan.compacted_items == 2 && plan.memory_index == 1);
  Q4T_CHECK(plan.retained.array.size() == 2 &&
            plan.retained.array[0].GetString("id") == "sys" &&
            plan.retained.array[1].GetString("id") == "u2");
  Q4T_CHECK(OutputJson(plan.source) == OutputJson(history));
  const auto* messages = plan.request.GetArray("messages");
  Q4T_CHECK(messages && messages->array.size() == 2);
  Q4T_CHECK(messages->array[1].GetString("content") == OutputJson(history));
  Q4T_CHECK(!plan.request.GetBool("enable_thinking", true) &&
            plan.request.GetInt("max_completion_tokens") == 256 &&
            !plan.request.Has("tools"));
  return true;
}

Q4T_TEST(compaction_plan_standalone_preserves_user_and_media_items) {
  auto history = History();
  // Media tool output requires its call to remain as well.
  history.array.insert(history.array.end() - 1, Call("call_media", "c1"));
  auto output = Output("output_media", "c1");
  Field(&output, "output") = Parse(R"([{"type":"input_image",
    "image_url":"data:image/png;base64,AA==","detail":"auto"}])");
  history.array.insert(history.array.end() - 1, output);
  const auto plan = Plan(history, ResponsesCompactionMode::kStandalone);
  Q4T_CHECK(plan.compacted_items == 1 && plan.retained.array.size() == 5);
  const auto retained = OutputJson(plan.retained);
  Q4T_CHECK(retained.find("call_media") != retained.npos &&
            retained.find("output_media") != retained.npos &&
            retained.find("Remember the result 42") != retained.npos &&
            retained.find("What was the result?") != retained.npos);
  return true;
}

Q4T_TEST(compaction_pending_parallel_tool_spans_are_not_summarized) {
  auto history = History();
  history.array.pop_back();
  history.array.push_back(Call("ca", "a"));
  history.array.push_back(Call("cb", "b"));
  history.array.push_back(Output("oa", "a"));
  const auto plan = Plan(history, ResponsesCompactionMode::kStandalone);
  Q4T_CHECK(plan.compacted_items == 1 && plan.memory_index == 2);
  Q4T_CHECK(plan.retained.array[2].GetString("id") == "ca" &&
            plan.retained.array[3].GetString("id") == "cb" &&
            plan.retained.array[4].GetString("id") == "oa");
  return true;
}

Q4T_TEST(compaction_invalid_tool_history_cannot_be_hidden_in_memory) {
  for (int mode = 0; mode < 4; ++mode) {
    auto history = History();
    auto call = Call("ca", "a");
    if (mode == 0) {
      history.array.push_back(Output("oa", "absent"));
    } else if (mode == 1) {
      String(&call, "arguments", "not JSON");
      history.array.push_back(call);
    } else if (mode == 2) {
      history.array.push_back(call);
      history.array.push_back(Call("cb", "a"));
    } else {
      history.array.push_back(call);
      auto output = Output("oa", "a");
      String(&output, "type", "custom_tool_call_output");
      history.array.push_back(output);
    }
    ResponsesCompactionPlan plan;
    ChatPlanError error;
    Q4T_CHECK(!PlanResponsesCompaction(history,
                                       ResponsesCompactionMode::kStandalone,
                                       256, Config(), &plan, &error));
    Q4T_CHECK(error.code == "invalid_request" && plan.source.IsNull());
  }
  return true;
}

Q4T_TEST(compaction_real_cipher_replays_full_retained_or_omitted_origin) {
  Fixture f;
  ObjectStore store(f.options);
  StateCipher cipher(store);
  const auto plan = Plan();
  Json item, expected;
  Q4T_CHECK(
      Ok(SealResponsesCompaction(cipher, plan, "Verified result: 42.", "cmp_1",
                                 Config(), Access(), &item, &expected)));
  Q4T_CHECK(expected.array.size() == 3 &&
            expected.array[0].GetString("id") == "sys" &&
            expected.array[1].GetString("role") == "assistant" &&
            expected.array[2].GetString("id") == "u2");
  Q4T_CHECK(ResponsesCompactionId(item.GetString("encrypted_content")) ==
            "cmp_1");
  for (const auto& prefix : {plan.source, plan.retained, Parse("[]")}) {
    Json expanded;
    Q4T_CHECK(Ok(OpenResponsesCompaction(cipher, prefix, item, Config(),
                                         Access(), &expanded)));
    Q4T_CHECK(OutputJson(expanded) == OutputJson(expected));
  }
  return true;
}

Q4T_TEST(compaction_cipher_accepts_optional_id_and_semantic_message_forms) {
  Fixture f;
  ObjectStore store(f.options);
  StateCipher cipher(store);
  const auto plan = Plan();
  Json item, expected;
  Q4T_CHECK(Ok(SealResponsesCompaction(cipher, plan, "42", "cmp_1", Config(),
                                       Access(), &item, &expected)));
  std::erase_if(item.object,
                [](const auto& field) { return field.first == "id"; });
  auto retained = plan.retained;
  for (auto& message : retained.array) {
    String(&message, "id", "different_" + message.GetString("id"));
    auto content = Parse(R"([{"type":"input_text","text":""}])");
    String(&content.array[0], "text",
           message.GetArray("content")->array[0].GetString("text"));
    Field(&message, "content") = content;
  }
  Json expanded;
  Q4T_CHECK(Ok(OpenResponsesCompaction(cipher, retained, item, Config(),
                                       Access(), &expanded)));
  Q4T_CHECK(OutputJson(expanded) == OutputJson(expected));
  return true;
}

Q4T_TEST(compaction_cipher_rejects_tamper_scope_model_id_and_prefix) {
  Fixture f;
  ObjectStore store(f.options);
  StateCipher cipher(store);
  const auto plan = Plan();
  Json item, expected;
  Q4T_CHECK(Ok(SealResponsesCompaction(cipher, plan, "42", "cmp_1", Config(),
                                       Access(), &item, &expected)));
  for (int mode = 0; mode < 5; ++mode) {
    auto bad = item;
    auto prefix = plan.retained;
    auto config = Config();
    auto access = Access();
    if (mode == 0) {
      auto text = bad.GetString("encrypted_content");
      text.back() = text.back() == '0' ? '1' : '0';
      String(&bad, "encrypted_content", text);
    } else if (mode == 1) {
      access = Access("client:other");
    } else if (mode == 2) {
      config.model = "other";
    } else if (mode == 3) {
      String(&bad, "id", "cmp_other");
    } else {
      String(&prefix.array[0], "content", "Replace the instructions");
    }
    auto expanded = Parse(R"({"untouched":true})");
    Q4T_CHECK(
        OpenResponsesCompaction(cipher, prefix, bad, config, access, &expanded)
            .code == "invalid_encrypted_content");
    Q4T_CHECK(expanded.GetBool("untouched"));
  }
  return true;
}

Q4T_TEST(compaction_noop_never_invents_model_memory_or_usage) {
  Fixture f;
  ObjectStore store(f.options);
  StateCipher cipher(store);
  auto history = Parse("[]");
  history.array.push_back(Message("u", "user", "Plan a trip"));
  const auto plan = Plan(history, ResponsesCompactionMode::kStandalone);
  Q4T_CHECK(plan.compacted_items == 0 && plan.request.IsNull());
  Json item, expected;
  Q4T_CHECK(Ok(SealResponsesCompaction(cipher, plan, "", "cmp_1", Config(),
                                       Access(), &item, &expected)));
  Q4T_CHECK(OutputJson(expected) == OutputJson(history));
  Q4T_CHECK(SealResponsesCompaction(cipher, plan, "invented", "cmp_2", Config(),
                                    Access(), &item, &expected)
                .code == "compaction_output_invalid");
  Q4T_CHECK(SealResponsesCompaction(cipher, Plan(), "  \n", "cmp_2", Config(),
                                    Access(), &item, &expected)
                .code == "compaction_output_invalid");
  return true;
}

Q4T_TEST(compaction_bounds_duplicate_ids_and_output_budget) {
  ResponsesCompactionPlan plan;
  ChatPlanError error;
  auto history = History();
  history.array.push_back(history.array.back());
  Q4T_CHECK(!PlanResponsesCompaction(history,
                                     ResponsesCompactionMode::kAutomatic, 256,
                                     Config(), &plan, &error));
  Q4T_CHECK(!PlanResponsesCompaction(History(),
                                     ResponsesCompactionMode::kAutomatic, 0,
                                     Config(), &plan, &error));
  Q4T_CHECK(error.code == "response_budget_invalid");
  history = History();
  String(&Field(&history.array[1], "content").array[0], "text",
         std::string(kCryptoPlaintextLimit + 1, 'x'));
  Q4T_CHECK(!PlanResponsesCompaction(history,
                                     ResponsesCompactionMode::kAutomatic, 256,
                                     Config(), &plan, &error));
  Q4T_CHECK(error.code == "compaction_source_limit");
  return true;
}

Q4T_TEST(compaction_cancelled_or_expired_state_never_mutates_output) {
  Fixture f;
  ObjectStore store(f.options);
  StateCipher cipher(store);
  for (const bool expired : {false, true}) {
    auto access = Access();
    if (expired)
      access.deadline = Clock::now() - 1s;
    else
      access.cancelled = [] { return true; };
    auto item = Parse(R"({"untouched":true})");
    auto expanded = item;
    const auto reply = SealResponsesCompaction(
        cipher, Plan(), "42", "cmp_1", Config(), access, &item, &expanded);
    Q4T_CHECK(reply.http_status == (expired ? 408 : 499));
    Q4T_CHECK(item.GetBool("untouched") && expanded.GetBool("untouched"));
    Q4T_CHECK(cipher.Describe(Access()).http_status == 404);
  }
  return true;
}

Q4T_TEST(compaction_optional_id_enters_actual_response_generation_plan) {
  Fixture f;
  ObjectStore store(f.options);
  StateCipher cipher(store);
  const auto plan = Plan();
  Json item, expanded;
  Q4T_CHECK(
      Ok(SealResponsesCompaction(cipher, plan, "Verified result: 42.", "cmp_1",
                                 Config(), Access(), &item, &expanded)));
  std::erase_if(item.object,
                [](const auto& field) { return field.first == "id"; });
  auto request = Parse(R"({"model":"cpu-model","store":false,
    "reasoning":{"effort":"none"},"input":[]})");
  Json input;
  ChatPlanError error;
  Q4T_CHECK(ResponsesCompactionWindow(plan.retained, item, &input, &error));
  input.array.push_back(
      Parse(R"({"role":"user","content":"Repeat the result"})"));
  Field(&request, "input") = input;
  ResponsesExecution run(store);
  Q4T_CHECK(Ok(run.Prepare(request, Config(), Access(), 42)));
  const auto prompt = OutputJson(run.Generation().input.messages);
  Q4T_CHECK(prompt.find("Verified result: 42.") != prompt.npos &&
            prompt.find("Repeat the result") != prompt.npos &&
            prompt.find("q4tc1:") == prompt.npos);
  Q4T_CHECK(run.Generation().input.items.array.size() == 4);
  // Public request input remains the opaque state; it was not rewritten.
  Q4T_CHECK(!request.GetArray("input")->array[2].Has("id"));
  return true;
}

Q4T_TEST(compaction_returned_window_is_replayable_without_losing_message_data) {
  const auto plan = Plan();
  const auto item = Parse(R"({"type":"compaction","id":"cmp_1",
    "encrypted_content":"opaque"})");
  Json window;
  ChatPlanError error;
  Q4T_CHECK(ResponsesCompactionWindow(plan.retained, item, &window, &error));
  Q4T_CHECK(window.array.size() == 3 && !window.array[0].Has("id") &&
            !window.array[1].Has("id") &&
            window.array[2].GetString("id") == "cmp_1");
  for (size_t i = 0; i < plan.retained.array.size(); ++i) {
    Q4T_CHECK(OutputJson(Field(&window.array[i], "content")) ==
              OutputJson(*plan.retained.array[i].Find("content")));
    Q4T_CHECK(window.array[i].GetString("role") ==
              plan.retained.array[i].GetString("role"));
  }
  Q4T_CHECK(plan.retained.array[0].GetString("id") == "sys");
  auto invalid = plan.retained;
  String(&invalid.array[0], "secret_field", "not in input contract");
  auto preserved = Parse(R"({"untouched":true})");
  Q4T_CHECK(!ResponsesCompactionWindow(invalid, item, &preserved, &error));
  Q4T_CHECK(preserved.GetBool("untouched"));
  return true;
}

Q4T_TEST(compaction_and_encrypted_reasoning_form_one_authenticated_history) {
  Fixture f;
  ObjectStore store(f.options);
  StateCipher cipher(store);
  const auto plan = Plan();
  Json compact, expanded;
  Q4T_CHECK(
      Ok(SealResponsesCompaction(cipher, plan, "Verified result: 42.", "cmp_1",
                                 Config(), Access(), &compact, &expanded)));
  auto output = Parse(R"([{"id":"reason_1","type":"reasoning",
    "status":"completed","summary":[],"content":[
      {"type":"reasoning_text","text":"The verified result is 42."}]}])");
  output.array.insert(output.array.begin(), compact);
  std::string opaque;
  Q4T_CHECK(Ok(SealResponsesReasoning(cipher, expanded, output, 1, Config(),
                                      Access(), &opaque)));
  auto input = plan.source;
  input.array.push_back(compact);
  auto reasoning = output.array[1];
  Field(&reasoning, "content") = Parse("[]");
  String(&reasoning, "encrypted_content", opaque);
  input.array.push_back(reasoning);
  Json restored;
  Q4T_CHECK(
      Ok(OpenResponsesReasoning(cipher, input, Config(), Access(), &restored)));
  Q4T_CHECK(restored.array.size() == expanded.array.size() + 1);
  Q4T_CHECK(restored.array.back().GetArray("content")->array[0].GetString(
                "text") == "The verified result is 42.");
  return true;
}

namespace {
Json ExecutionInput() {
  auto history = History();
  for (auto& item : history.array)
    if (item.GetString("role") != "assistant")
      std::erase_if(item.object,
                    [](const auto& field) { return field.first == "id"; });
  return history;
}
Json ExecutionRequest(bool automatic = true) {
  auto request = Parse(R"({"model":"cpu-model","store":true,
    "max_output_tokens":128,"reasoning":{"effort":"none"}})");
  Field(&request, "input") = ExecutionInput();
  if (automatic)
    Field(&request, "context_management") =
        Parse(R"([{"type":"compaction","compact_threshold":600}])");
  return request;
}
ResponsesExecution::ContextMeasure Measured(size_t tokens) {
  return
      [tokens](const ResponsesGenerationPlan&, size_t* count, ChatPlanError*) {
        *count = tokens;
        return q4t::Status();
      };
}
void SaveCompaction(std::string_view name, std::string_view wire) {
  const char* directory = std::getenv("Q4T_TEST_OUTPUT_DIR");
  if (!directory) return;
  const auto root = std::filesystem::path(directory) / "compaction-wire";
  std::filesystem::create_directories(root);
  std::ofstream file(root / (std::string(name) + ".wire"));
  file << wire;
  if (!file.good()) throw std::runtime_error("cannot export compaction wire");
}
size_t Count(std::string_view wire, std::string_view value) {
  size_t count = 0, pos = 0;
  while ((pos = wire.find(value, pos)) != wire.npos) {
    ++count;
    pos += value.size();
  }
  return count;
}
}  // namespace

Q4T_TEST(compaction_request_shape_is_separate_and_stateless) {
  ChatPlanError error;
  Json normalized;
  Q4T_CHECK(NormalizeResponsesCompactRequest(
      Parse(R"({"model":null,"input":"hello","instructions":"Be brief"})"),
      Config(), &normalized, &error));
  Q4T_CHECK(normalized.GetString("model") == "cpu-model" &&
            !normalized.GetBool("store", true) &&
            normalized.GetString("instructions") == "Be brief");
  for (const auto* field :
       {"store", "stream", "background", "max_output_tokens", "tools"}) {
    auto request = Parse(R"({"model":"cpu-model","input":"hello"})");
    Field(&request, field) = Parse("null");
    Q4T_CHECK(!NormalizeResponsesCompactRequest(request, Config(), &normalized,
                                                &error));
    Q4T_CHECK(error.param == field);
  }
  Q4T_CHECK(!NormalizeResponsesCompactRequest(Parse(R"({"input":"hello"})"),
                                              Config(), &normalized, &error));
  Q4T_CHECK(error.param == "model");
  return true;
}

Q4T_TEST(compaction_execution_threshold_and_plain_requests_do_not_load_keys) {
  Fixture f;
  ObjectStore objects(f.options);
  ResponsesExecution below(objects), plain(objects), above(objects);
  Q4T_CHECK(Ok(below.Prepare(ExecutionRequest(), Config(), Access(), 10)));
  Q4T_CHECK(Ok(below.SelectCompaction(599, Config())));
  Q4T_CHECK(!below.CompactionSelected());
  Q4T_CHECK(Ok(plain.Prepare(ExecutionRequest(false), Config(), Access(), 10)));
  Q4T_CHECK(Ok(plain.SelectCompaction(7000, Config())));
  Q4T_CHECK(!plain.CompactionSelected());
  Q4T_CHECK(objects.Health().workers_started == 0);
  Q4T_CHECK(Ok(above.Prepare(ExecutionRequest(), Config(), Access(), 10)));
  Q4T_CHECK(Ok(above.SelectCompaction(600, Config())));
  Q4T_CHECK(above.CompactionSelected() &&
            above.Compaction().request.GetInt("max_completion_tokens") == 64);
  return true;
}

Q4T_TEST(compaction_execution_trigger_is_not_a_model_message) {
  Fixture f;
  ObjectStore objects(f.options);
  auto request = ExecutionRequest(false);
  Field(&request, "input")
      .array.push_back(Parse(R"({"type":"compaction_trigger"})"));
  ResponsesExecution run(objects);
  Q4T_CHECK(Ok(run.Prepare(request, Config(), Access(), 10)));
  Q4T_CHECK(OutputJson(run.Generation().generation_request)
                .find("compaction_trigger") == std::string::npos);
  Q4T_CHECK(Ok(run.SelectCompaction(10, Config())) && run.CompactionSelected());
  Q4T_CHECK(run.Compaction().source.array.size() == 4);
  return true;
}

Q4T_TEST(compaction_output_lifecycle_has_one_done_and_no_tool_events) {
  Fixture f;
  ObjectStore objects(f.options);
  ResponsesExecution run(objects);
  Q4T_CHECK(Ok(run.Prepare(ExecutionRequest(), Config(), Access(), 10)));
  Q4T_CHECK(Ok(run.SelectCompaction(600, Config())));
  std::string wire, events;
  Q4T_CHECK(Ok(run.Begin(&wire)));
  events += wire;
  Q4T_CHECK(Ok(run.StartCompaction(&wire)));
  events += wire;
  Q4T_CHECK(wire.find("response.compaction.compacting") != wire.npos);
  const auto event_begin = wire.find("data: ", wire.find(
      "event: response.compaction.compacting"));
  const auto event_end = wire.find("\r\n", event_begin);
  Q4T_CHECK(!Parse(wire.substr(event_begin + 6, event_end - event_begin - 6))
                 .Has("obfuscation"));
  Q4T_CHECK(!run.Finish(false, 0, {}, 11, &wire).ok());
  Q4T_CHECK(Ok(run.CompleteCompaction("Verified result: 42.", 12, Config(),
                                      Measured(80), &wire)));
  events += wire;
  Q4T_CHECK(run.Generation().generation_request.GetInt(
                "max_completion_tokens") == 116);
  ResponseDelta answer;
  answer.kind = ResponseDelta::Kind::kContent;
  answer.text = "42";
  Q4T_CHECK(run.Append(answer, "null", &wire));
  events += wire;
  Q4T_CHECK(Ok(run.Finish(false, 0, {680, 0, 14, 12}, 11, &wire)));
  events += wire;
  Q4T_CHECK(Count(events, "event: response.output_item.done\r\n") == 2);
  Q4T_CHECK(Count(events, "event: response.compaction.compacting\r\n") == 1);
  Q4T_CHECK(events.find("function_call_arguments.done") == events.npos);
  SaveCompaction("automatic-events", events);
  SaveCompaction("automatic-result", run.ResultJson());
  const auto& output = *run.Output().Snapshot().GetArray("output");
  Q4T_CHECK(output.array[0].GetString("type") == "compaction" &&
            !output.array[0].Has("status") &&
            !output.array[0].GetString("encrypted_content").empty());
  Q4T_CHECK(run.Output().Snapshot().Find("usage")->GetInt("total_tokens") ==
            694);
  return true;
}

Q4T_TEST(compaction_execution_does_not_accept_nonreducing_memory) {
  Fixture f;
  ObjectStore objects(f.options);
  ResponsesExecution run(objects);
  Q4T_CHECK(Ok(run.Prepare(ExecutionRequest(), Config(), Access(), 10)));
  Q4T_CHECK(Ok(run.SelectCompaction(600, Config())));
  const auto original = OutputJson(run.Generation().generation_request);
  std::string wire;
  Q4T_CHECK(Ok(run.Begin(&wire)) && Ok(run.StartCompaction(&wire)));
  const auto reply =
      run.CompleteCompaction("memory", 12, Config(), Measured(600), &wire);
  Q4T_CHECK(reply.code == "compaction_not_reduced" && wire.empty());
  Q4T_CHECK(OutputJson(run.Generation().generation_request) == original);
  Q4T_CHECK(run.Output()
                .Snapshot()
                .GetArray("output")
                ->array[0]
                .GetString("encrypted_content")
                .empty());
  ResponsesExecution::ObserveError(&run, reply.http_status, reply.message,
                                   reply.code);
  Q4T_CHECK(Ok(run.CloseUnfinished()));
  Q4T_CHECK(run.Output().Snapshot().GetString("status") == "failed");
  Q4T_CHECK(
      !run.Output().Snapshot().GetArray("output")->array[0].Has("status"));
  return true;
}

Q4T_TEST(compaction_execution_measurement_failure_preserves_original_window) {
  Fixture f;
  ObjectStore objects(f.options);
  ResponsesExecution run(objects);
  Q4T_CHECK(Ok(run.Prepare(ExecutionRequest(), Config(), Access(), 10)));
  Q4T_CHECK(Ok(run.SelectCompaction(600, Config())));
  std::string wire;
  Q4T_CHECK(Ok(run.Begin(&wire)) && Ok(run.StartCompaction(&wire)));
  const auto original = OutputJson(run.Generation().input.items);
  const auto reply = run.CompleteCompaction(
      "memory", 12, Config(),
      [](const ResponsesGenerationPlan&, size_t*, ChatPlanError* error) {
        *error = {"context_length_exceeded", "input", 400};
        return q4t::Status::Fail("expanded media does not fit");
      },
      &wire);
  Q4T_CHECK(reply.code == "context_length_exceeded" && wire.empty());
  Q4T_CHECK(OutputJson(run.Generation().input.items) == original);
  Q4T_CHECK(Ok(run.CloseUnfinished()));
  return true;
}

Q4T_TEST(compaction_execution_cancellation_after_measurement_has_no_done) {
  Fixture f;
  ObjectStore objects(f.options);
  ResponsesExecution run(objects);
  Q4T_CHECK(Ok(run.Prepare(ExecutionRequest(), Config(), Access(), 10)));
  Q4T_CHECK(Ok(run.SelectCompaction(600, Config())));
  bool cancelled = false;
  run.BindCancellation(Clock::now() + 5s, [&] { return cancelled; });
  std::string wire;
  Q4T_CHECK(Ok(run.Begin(&wire)) && Ok(run.StartCompaction(&wire)));
  const auto reply = run.CompleteCompaction(
      "memory", 12, Config(),
      [&](const ResponsesGenerationPlan&, size_t* count, ChatPlanError*) {
        *count = 80;
        cancelled = true;
        return q4t::Status();
      },
      &wire);
  Q4T_CHECK(reply.code == "request_cancelled" && wire.empty());
  Q4T_CHECK(Ok(run.CloseUnfinished()));
  return true;
}

Q4T_TEST(responses_pro_compaction_and_summary_share_actual_remaining_budget) {
  Fixture f;
  ObjectStore objects(f.options);
  auto request = ExecutionRequest();
  Field(&request, "reasoning") =
      Parse(R"({"mode":"pro","effort":"none","summary":"concise"})");
  ResponsesExecution run(objects);
  Q4T_CHECK(Ok(run.Prepare(request, Config(), Access(), 10)));
  const auto original = OutputJson(run.Generation().input.items);
  Q4T_CHECK(Ok(run.SelectCompaction(600, Config())));
  std::string wire;
  Q4T_CHECK(Ok(run.Begin(&wire)) && Ok(run.StartCompaction(&wire)));
  Q4T_CHECK(Ok(run.CompleteCompaction("Verified result is 42", 24, Config(),
                                     Measured(80), &wire)));
  const auto compacted = OutputJson(run.Generation().input.items);
  Q4T_CHECK(compacted != original);
  ResponsesSummaryPlan work;
  ChatPlanError error;
  Q4T_CHECK(PlanResponsesDeliberation(run.Generation(), 128, 24, Config(),
                                      &work, &error));
  Q4T_CHECK(work.request.GetInt("max_completion_tokens") == 34 &&
            OutputJson(work.request).find("Verified result is 42") !=
                std::string::npos);
  ResponsesGenerationPlan answer;
  Q4T_CHECK(PlanResponsesDeliberatedAnswer(
      run.Generation(), "The recorded result is 42", 128, 54, Config(),
      &answer, &error));
  Q4T_CHECK(answer.generation_request.GetInt("max_completion_tokens") == 74 &&
            OutputJson(answer.input.items) == compacted);
  ResponseDelta note;
  note.kind = ResponseDelta::Kind::kReasoning;
  note.text = "The recorded result is 42";
  Q4T_CHECK(run.Append(note, "null", &wire));
  auto final = note;
  final.kind = ResponseDelta::Kind::kContent;
  final.text = "42";
  Q4T_CHECK(run.Append(final, "null", &wire));
  ResponsesSummaryPlan summary;
  Q4T_CHECK(PlanResponsesSummary(run.Envelope(), run.Output().Snapshot(),
                                 128, 60, Config(), &summary, &error));
  Q4T_CHECK(summary.needed &&
            summary.request.GetInt("max_completion_tokens") == 68 &&
            OutputJson(summary.request).find(note.text) != std::string::npos);
  Q4T_CHECK(run.AppendSummary("Checked the actual recorded result", &wire));
  Q4T_CHECK(Ok(run.Finish(false, 0, {600, 0, 70, 64}, 11, &wire)));
  Q4T_CHECK(run.Output().Snapshot().GetString("status") == "completed");
  return true;
}

Q4T_TEST(compaction_execution_budget_cannot_refill_or_skip_primary) {
  Fixture f;
  ObjectStore objects(f.options);
  auto request = ExecutionRequest();
  Field(&request, "max_output_tokens") = Parse("1");
  ResponsesExecution tiny(objects), spent(objects);
  Q4T_CHECK(Ok(tiny.Prepare(request, Config(), Access(), 10)));
  Q4T_CHECK(tiny.SelectCompaction(600, Config()).code ==
            "compaction_budget_exhausted");
  Q4T_CHECK(!tiny.CompactionSelected());
  Q4T_CHECK(Ok(spent.Prepare(ExecutionRequest(), Config(), Access(), 10)));
  Q4T_CHECK(Ok(spent.SelectCompaction(600, Config())));
  std::string wire;
  Q4T_CHECK(Ok(spent.Begin(&wire)) && Ok(spent.StartCompaction(&wire)));
  Q4T_CHECK(
      spent.CompleteCompaction("memory", 128, Config(), Measured(80), &wire)
          .code == "compaction_budget_exhausted");
  Q4T_CHECK(wire.empty() && Ok(spent.CloseUnfinished()));
  return true;
}

Q4T_TEST(compaction_standalone_returns_preserved_users_and_replayable_state) {
  Fixture f;
  ObjectStore objects(f.options);
  auto request =
      Parse(R"({"model":"cpu-model","instructions":"Preserve facts"})");
  Field(&request, "input") = ExecutionInput();
  ResponsesExecution run(objects, nullptr, false, true);
  Q4T_CHECK(Ok(run.Prepare(request, Config(), Access(), 10)));
  Q4T_CHECK(!run.Envelope().store && !run.Envelope().stream);
  Q4T_CHECK(Ok(run.SelectCompaction(600, Config())));
  Q4T_CHECK(run.Compaction().compacted_items == 1 &&
            OutputJson(run.Compaction().request).find("Preserve facts") !=
                std::string::npos);
  std::string wire;
  Q4T_CHECK(Ok(run.Begin(&wire)) && Ok(run.StartCompaction(&wire)));
  Q4T_CHECK(Ok(run.CompleteCompaction("Verified result: 42.", 12, Config(),
                                      Measured(150), &wire)));
  Q4T_CHECK(Ok(run.Finish(false, 0, {600, 0, 12, 12}, 11, &wire)));
  SaveCompaction("standalone-result", run.ResultJson());
  const auto result = Parse(run.ResultJson());
  Q4T_CHECK(result.GetString("object") == "response.compaction" &&
            !result.Has("status") && !result.Has("store"));
  const auto& output = *result.GetArray("output");
  Q4T_CHECK(output.array.size() == 4 &&
            output.array[1].GetString("role") == "user" &&
            !output.array[1].Has("id"));
  ResponsesExecution resumed(objects);
  auto next = ExecutionRequest(false);
  Field(&next, "input") = output;
  Q4T_CHECK(Ok(resumed.Prepare(next, Config(), Access(), 12)));
  Q4T_CHECK(OutputJson(resumed.Generation().input.items)
                .find("Verified result: 42.") != std::string::npos);
  return true;
}

Q4T_TEST(compaction_standalone_noop_is_honest_zero_generation) {
  Fixture f;
  ObjectStore objects(f.options);
  ResponsesExecution run(objects, nullptr, false, true);
  Q4T_CHECK(Ok(run.Prepare(Parse(R"({"model":"cpu-model","input":"hello"})"),
                           Config(), Access(), 10)));
  Q4T_CHECK(Ok(run.SelectCompaction(10, Config())));
  Q4T_CHECK(run.Compaction().compacted_items == 0 &&
            run.Compaction().request.IsNull());
  std::string wire;
  Q4T_CHECK(Ok(run.Begin(&wire)) && Ok(run.StartCompaction(&wire)));
  Q4T_CHECK(Ok(run.CompleteCompaction("", 0, Config(), Measured(10), &wire)));
  Q4T_CHECK(Ok(run.Finish(false, 0, {}, 11, &wire)));
  const auto result = Parse(run.ResultJson());
  Q4T_CHECK(result.Find("usage")->GetInt("total_tokens", -1) == 0 &&
            result.GetArray("output")->array.size() == 2);
  return true;
}

Q4T_TEST(compaction_stored_chain_preserves_history_and_removes_old_trigger) {
  Fixture f;
  ObjectStore objects(f.options);
  auto request = ExecutionRequest(false);
  Field(&request, "input")
      .array.push_back(Parse(R"({"type":"compaction_trigger"})"));
  ResponsesExecution run(objects);
  Q4T_CHECK(Ok(run.Prepare(request, Config(), Access(), 10)));
  Q4T_CHECK(Ok(run.SelectCompaction(600, Config())));
  std::string wire;
  Q4T_CHECK(Ok(run.Begin(&wire)) && Ok(run.StartCompaction(&wire)));
  Q4T_CHECK(Ok(run.CompleteCompaction("Verified result: 42.", 12, Config(),
                                      Measured(80), &wire)));
  ResponseDelta answer;
  answer.kind = ResponseDelta::Kind::kContent;
  answer.text = "42";
  Q4T_CHECK(run.Append(answer, "null", &wire));
  Q4T_CHECK(Ok(run.Finish(false, 0, {680, 0, 14, 12}, 11, &wire)));
  ResponsesState state(objects);
  const auto record = state.ReadRecord(run.Id(), Access());
  Q4T_CHECK(Ok(record));
  Q4T_CHECK(record.value.GetArray("history")->array.size() == 5 &&
            record.value.GetArray("history")->array.back().GetString("type") ==
                "compaction_trigger");
  auto next = Parse(R"({"model":"cpu-model","input":"What was the result?"})");
  String(&next, "previous_response_id", run.Id());
  ResponsesExecution resumed(objects);
  Q4T_CHECK(Ok(resumed.Prepare(next, Config(), Access(), 12)));
  const auto actual = OutputJson(resumed.Generation().generation_request);
  Q4T_CHECK(actual.find("Verified result: 42.") != std::string::npos &&
            actual.find("compaction_trigger") == std::string::npos);
  Q4T_CHECK(Ok(resumed.SelectCompaction(100, Config())) &&
            !resumed.CompactionSelected());
  return true;
}

Q4T_TEST(
    compaction_background_checkpoint_and_cancel_never_finish_pending_item) {
  Fixture f;
  ObjectStore objects(f.options);
  auto request = ExecutionRequest();
  Field(&request, "background") = Parse("true");
  ResponsesExecution queued(objects);
  Q4T_CHECK(Ok(queued.Prepare(request, Config(), Access(), 10, true)));
  std::string wire;
  Q4T_CHECK(Ok(queued.Begin(&wire)));
  ResponsesExecution run(objects);
  Q4T_CHECK(Ok(run.RestoreQueued(queued.Id(), Config(), Access())));
  Q4T_CHECK(Ok(run.StartRunning(&wire)));
  Q4T_CHECK(Ok(run.SelectCompaction(600, Config())));
  Q4T_CHECK(Ok(run.StartCompaction(&wire)) && Ok(run.Checkpoint()));
  Q4T_CHECK(Ok(run.Cancel()));
  Q4T_CHECK(run.Output().Snapshot().GetString("status") == "cancelled");
  Q4T_CHECK(!run.CompleteCompaction("memory", 12, Config(), Measured(80), &wire)
                 .ok());
  Q4T_CHECK(wire.empty() && Ok(run.CloseUnfinished()));
  return true;
}

Q4T_TEST(compaction_route_is_generation_not_observation_or_cancellation) {
  Q4T_CHECK(IsResponsesCompaction("/v1/responses/compact"));
  Q4T_CHECK(ResponsesMethodAllowed("POST", "/v1/responses/compact"));
  Q4T_CHECK(!ResponsesMethodAllowed("GET", "/v1/responses/compact"));
  Q4T_CHECK(!ResponsesMethodAllowed("DELETE", "/v1/responses/compact"));
  Q4T_CHECK(!IsResponsesObservation("GET", "/v1/responses/compact"));
  Q4T_CHECK(!IsResponseCancellation("POST", "/v1/responses/compact"));
  ResponsesReadPlan plan;
  const auto reply =
      PlanResponsesRead("GET", "/v1/responses/compact", "", &plan);
  Q4T_CHECK(reply.http_status == 405);
  return true;
}

Q4T_TEST(compaction_window_capacity_never_returns_an_unreplayable_result) {
  auto retained = Parse("[]");
  retained.array.resize(4096, Message("u", "user", "hello"));
  const auto item = Parse(R"({"type":"compaction","id":"cmp_1",
    "encrypted_content":"opaque"})");
  auto window = Parse("[42]");
  ChatPlanError error;
  Q4T_CHECK(!ResponsesCompactionWindow(retained, item, &window, &error));
  Q4T_CHECK(error.code == "compaction_output_limit" &&
            OutputJson(window) == "[42]");
  return true;
}
