#include "q4t/server/responses_compaction.h"

#include <unistd.h>

#include <algorithm>
#include <cstdlib>
#include <filesystem>

#include "q4t/server/output_schema.h"
#include "q4t/server/responses_crypto.h"
#include "q4t/server/responses_execution.h"
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
