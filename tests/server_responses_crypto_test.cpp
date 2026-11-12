#include "q4t/server/responses_crypto.h"

#include <unistd.h>

#include <cstdlib>
#include <filesystem>
#include <fstream>

#include "q4t/server/conversation_state.h"
#include "q4t/server/output_schema.h"
#include "q4t/server/responses_execution.h"
#include "q4t/test.h"

namespace {
using namespace q4t::server;
using q4t::io::Json;
using Clock = std::chrono::steady_clock;
using namespace std::chrono_literals;
Json Parse(const std::string& raw) {
  Json result;
  if (!q4t::io::ParseJson(raw, &result, true))
    throw std::runtime_error("reasoning cipher fixture JSON");
  return result;
}
Json& Field(Json* object, const char* name) {
  for (auto& [key, value] : object->object)
    if (key == name) return value;
  object->object.emplace_back(name, Json());
  return object->object.back().second;
}
void String(Json* object, const char* name, const std::string& text) {
  auto& field = Field(object, name);
  field = {};
  field.type = Json::Type::kString;
  field.str = text;
}
struct Fixture {
  ObjectStoreOptions options;
  Fixture() {
    const char* output = std::getenv("Q4T_TEST_OUTPUT_DIR");
    auto root = std::filesystem::absolute(
        output ? output : ".q4t-work/responses-crypto-cpu");
    std::filesystem::create_directories(root);
    auto name = (root / "reasoning-XXXXXX").string();
    if (!mkdtemp(name.data()))
      throw std::runtime_error("reasoning fixture path");
    options.directory = name + "/objects";
    options.restart_backoff_ms = 0;
  }
};
StateAccess Access(std::string scope = "client:crypto") {
  return {std::move(scope), Clock::now() + 5s, {}};
}
ChatPlanConfig Config() {
  ChatPlanConfig config;
  config.model = "cpu-model";
  config.model_aliases = {"alias"};
  return config;
}
bool Ok(const ObjectReply& reply) {
  if (!reply.ok())
    std::fprintf(stderr, "%s: %s\n", reply.code.c_str(), reply.message.c_str());
  return reply.ok();
}
ResponseDelta Text(std::string text, bool reasoning = false) {
  ResponseDelta delta;
  delta.kind = reasoning ? ResponseDelta::Kind::kReasoning
                         : ResponseDelta::Kind::kContent;
  delta.text = std::move(text);
  return delta;
}
Json Request() {
  return Parse(R"({"input":"Compute 17+25","model":"alias","store":false,
    "reasoning":{"effort":"low"},"stream":true,
    "stream_options":{"include_obfuscation":false},
    "include":["reasoning.encrypted_content"]})");
}
Json OpaqueOnly(Json item) {
  std::erase_if(item.object,
                [](const auto& field) { return field.first == "content"; });
  Field(&item, "summary") = Parse("[]");
  return item;
}
Json Follow(Json input) {
  auto request = Request();
  Field(&request, "input") = std::move(input);
  return request;
}
Json Generated(ObjectStore& store, Json request = Request(),
               bool summary = false, bool tool = false,
               bool incomplete = false) {
  ResponsesExecution run(store);
  if (!Ok(run.Prepare(request, Config(), Access(), 42)))
    throw std::runtime_error("reasoning fixture prepare");
  std::string wire, part;
  if (!Ok(run.Begin(&wire)) ||
      !run.Append(Text("17+25 equals 42.", true), "null", &part))
    throw std::runtime_error("reasoning fixture begin");
  wire += part;
  if (tool) {
    ResponseDelta call;
    call.kind = ResponseDelta::Kind::kTool;
    call.tool_start = true;
    call.call = {0, "call_doc", "read_record", "{}", false};
    if (!run.Append(call, "null", &part))
      throw std::runtime_error("reasoning fixture tool");
  } else if (!incomplete && !run.Append(Text("42"), "null", &part)) {
    throw std::runtime_error("reasoning fixture answer");
  }
  if (!incomplete) wire += part;
  if (summary) {
    if (!run.AppendSummary("The addition yields 42.", &part))
      throw std::runtime_error("reasoning fixture summary");
    wire += part;
  }
  if (!Ok(run.Finish(incomplete, tool ? 1 : 0, {9, 0, 8, 5}, 43, &part)))
    throw std::runtime_error("reasoning fixture finish");
  wire += part;
  if (const auto* out = std::getenv("Q4T_TEST_OUTPUT_DIR"); out && summary) {
    const auto root = std::filesystem::path(out) / "encrypted-renderer";
    std::filesystem::create_directories(root);
    std::ofstream(root / "response.json") << run.Output().Json() << '\n';
    std::ofstream(root / "response.wire") << wire;
  }
  return run.Output().Snapshot();
}
}  // namespace

Q4T_TEST(responses_crypto_stateless_roundtrip_restores_actual_reasoning) {
  Fixture f;
  ObjectStore store(f.options);
  const auto generated = Generated(store);
  Q4T_CHECK(ResponsesState(store)
                .Retrieve(generated.GetString("id"), Access())
                .http_status == 404);
  auto input = Parse("[]");
  input.array.push_back(OpaqueOnly(generated.GetArray("output")->array[0]));
  input.array.push_back(generated.GetArray("output")->array[1]);
  input.array.push_back(Parse(R"({"role":"user","content":"Explain"})"));
  ResponsesExecution follow(store);
  Q4T_CHECK(Ok(follow.Prepare(Follow(input), Config(), Access(), 44)));
  const auto& items = follow.Generation().input.items;
  Q4T_CHECK(items.array.size() == 4);
  Q4T_CHECK(OutputJson(items).find("Compute 17+25") != std::string::npos);
  Q4T_CHECK(items.array[1].GetArray("content")->array[0].GetString("text") ==
            "17+25 equals 42.");
  Q4T_CHECK(!items.array[1].Has("encrypted_content"));
  const auto messages = OutputJson(follow.Generation().input.messages);
  Q4T_CHECK(messages.find("17+25 equals 42.") != messages.npos &&
            messages.find("q4t1:") == messages.npos);
  return true;
}

Q4T_TEST(responses_crypto_origin_prefix_full_suffix_and_conflict) {
  Fixture f;
  ObjectStore store(f.options);
  auto request = Request();
  Field(&request, "input") = Parse(R"([{"role":"user","content":"old"},
    {"role":"assistant","content":"answer"},
    {"role":"user","content":"Compute 17+25"}])");
  const auto generated = Generated(store, request);
  for (size_t omitted = 0; omitted <= 3; ++omitted) {
    auto history = *request.GetArray("input");
    history.array.erase(history.array.begin(), history.array.begin() + omitted);
    history.array.push_back(OpaqueOnly(generated.GetArray("output")->array[0]));
    history.array.push_back(generated.GetArray("output")->array[1]);
    ResponsesExecution follow(store);
    Q4T_CHECK(Ok(follow.Prepare(Follow(history), Config(), Access(), 44)));
    Q4T_CHECK(follow.Generation().input.items.array.size() == 5);
  }
  auto history = *request.GetArray("input");
  String(&history.array[0], "content", "different origin");
  history.array.push_back(generated.GetArray("output")->array[0]);
  ResponsesExecution bad(store);
  const auto result = bad.Prepare(Follow(history), Config(), Access(), 44);
  Q4T_CHECK(result.code == "invalid_encrypted_content" && !bad.Begun());
  return true;
}

Q4T_TEST(responses_crypto_sse_summary_and_persisted_events_share_ciphertext) {
  Fixture f;
  ObjectStore store(f.options);
  auto request = Request();
  Field(&request, "store") = Parse("true");
  const auto response = Generated(store, request, true);
  const auto& output = response.GetArray("output")->array;
  Q4T_CHECK(output.size() == 3 && output[0].Has("encrypted_content") &&
            output[2].Has("encrypted_content") &&
            !output[1].Has("encrypted_content"));
  Q4T_CHECK(output[0].GetString("encrypted_content") !=
            output[2].GetString("encrypted_content"));
  const auto saved =
      ResponsesState(store).ReadRecord(response.GetString("id"), Access());
  Q4T_CHECK(Ok(saved));
  const auto& events = saved.value.GetArray("events")->array;
  size_t done = 0;
  for (size_t i = 0; i < events.size(); ++i) {
    Q4T_CHECK(events[i].GetInt("sequence_number") == static_cast<int64_t>(i));
    if (events[i].GetString("type") != "response.output_item.done") continue;
    const auto* item = events[i].Find("item");
    Q4T_CHECK(item && OutputJson(*item) == OutputJson(output[done++]));
  }
  Q4T_CHECK(done == output.size() &&
            events.back().GetString("type") == "response.completed");
  Q4T_CHECK(OutputJson(*events.back().Find("response")) ==
            OutputJson(response));
  auto input = Parse("[]");
  for (const auto& item : output)
    input.array.push_back(
        item.GetString("type") == "reasoning" ? OpaqueOnly(item) : item);
  ResponsesExecution follow(store);
  Q4T_CHECK(Ok(follow.Prepare(Follow(input), Config(), Access(), 44)));
  Q4T_CHECK(follow.Generation().input.items.array.size() == 4);
  Q4T_CHECK(OutputJson(follow.Generation().input.messages)
                .find("The addition yields 42.") != std::string::npos);
  return true;
}

Q4T_TEST(responses_crypto_previous_response_survives_store_restart) {
  Fixture f;
  std::string id;
  {
    ObjectStore store(f.options);
    auto request = Request();
    Field(&request, "store") = Parse("true");
    id = Generated(store, request).GetString("id");
  }
  ObjectStore store(f.options);
  auto request = Request();
  String(&request, "previous_response_id", id);
  String(&request, "input", "Follow up");
  ResponsesExecution run(store);
  Q4T_CHECK(Ok(run.Prepare(request, Config(), Access(), 44)));
  Q4T_CHECK(run.Generation().input.items.array.size() == 4);
  Q4T_CHECK(
      OutputJson(run.Generation().input.messages).find("17+25 equals 42.") !=
      std::string::npos);
  return true;
}

Q4T_TEST(responses_crypto_conversation_restores_without_mutating_stored_items) {
  Fixture f;
  ObjectStore store(f.options);
  ConversationState conversations(store);
  const auto created = conversations.Create(Parse("{}"), Parse("[]"), Access());
  Q4T_CHECK(Ok(created));
  const auto id = created.value.GetString("id");
  auto request = Request();
  String(&request, "conversation", id);
  Field(&request, "store") = Parse("true");
  const auto response = Generated(store, request);
  ItemViewOptions view;
  view.include = {"reasoning.encrypted_content"};
  const auto before = conversations.ListItems(id, view, Access());
  Q4T_CHECK(Ok(before));
  String(&request, "input", "Continue");
  ResponsesExecution run(store);
  Q4T_CHECK(Ok(run.Prepare(request, Config(), Access(), 44)));
  Q4T_CHECK(run.Generation().input.items.array.size() == 4);
  const auto after = conversations.ListItems(id, view, Access());
  Q4T_CHECK(Ok(after) && OutputJson(before.value) == OutputJson(after.value));
  return true;
}

Q4T_TEST(responses_crypto_background_queued_restore_uses_persistent_keys) {
  Fixture f;
  std::string id;
  {
    ObjectStore store(f.options);
    auto request = Request();
    Field(&request, "background") = Parse("true");
    ResponsesExecution run(store);
    Q4T_CHECK(Ok(run.Prepare(request, Config(), Access(), 42, true)));
    std::string wire;
    Q4T_CHECK(Ok(run.Begin(&wire)));
    id = run.Id();
  }
  ObjectStore store(f.options);
  ResponsesExecution run(store);
  Q4T_CHECK(Ok(run.RestoreQueued(id, Config(), Access())));
  std::string wire;
  Q4T_CHECK(Ok(run.StartRunning(&wire)));
  Q4T_CHECK(run.Append(Text("Reasoning", true), "null", &wire));
  Q4T_CHECK(run.Append(Text("42"), "null", &wire));
  Q4T_CHECK(Ok(run.Finish(false, 0, {3, 0, 2, 1}, 44, &wire)));
  const auto& output = *run.Output().Snapshot().GetArray("output");
  Q4T_CHECK(!output.array[0].GetString("encrypted_content").empty());
  ResponsesExecution follow(store);
  Q4T_CHECK(Ok(follow.Prepare(Follow(output), Config(), Access(), 45)));
  Q4T_CHECK(follow.Generation().input.items.array.size() == 3);
  return true;
}

Q4T_TEST(responses_crypto_wrong_owner_model_and_plaintext_are_rejected) {
  Fixture f;
  ObjectStore store(f.options);
  const auto response = Generated(store);
  for (int mode = 0; mode < 4; ++mode) {
    auto input = *response.GetArray("output");
    auto access = Access();
    auto config = Config();
    if (mode == 0) access.scope = "client:other";
    if (mode == 1) config.model = "other-model";
    if (mode == 2) {
      auto& blob = Field(&input.array[0], "encrypted_content").str;
      blob.back() = blob.back() == '0' ? '1' : '0';
    }
    if (mode == 3)
      String(&Field(&input.array[0], "content").array[0], "text",
             "forged reasoning");
    auto request = Follow(input);
    String(&request, "model", config.model);
    ResponsesExecution follow(store);
    const auto reply = follow.Prepare(request, config, access, 44);
    Q4T_CHECK(reply.code == "invalid_encrypted_content" && !follow.Begun());
    Q4T_CHECK(reply.value.GetString("param") == "input[0].encrypted_content");
  }
  return true;
}

Q4T_TEST(responses_crypto_restored_tool_context_does_not_execute_tools) {
  Fixture f;
  ObjectStore store(f.options);
  auto request = Request();
  const auto tool = Generated(store, request, false, true);
  auto input = *tool.GetArray("output");
  input.array[0] = OpaqueOnly(input.array[0]);
  input.array.push_back(
      Parse(R"({"type":"function_call_output","call_id":"call_doc",
    "output":"# Model architecture"})"));
  ResponsesExecution follow(store);
  Q4T_CHECK(Ok(follow.Prepare(Follow(input), Config(), Access(), 44)));
  const auto text = OutputJson(follow.Generation().input.messages);
  Q4T_CHECK(text.find("# Model architecture") != text.npos &&
            text.find("call_doc") != text.npos && !follow.Begun());
  Q4T_CHECK(follow.Output().Snapshot().GetArray("output")->array.empty());
  return true;
}

Q4T_TEST(responses_crypto_incomplete_reasoning_is_not_promoted_to_complete) {
  Fixture f;
  ObjectStore store(f.options);
  const auto response = Generated(store, Request(), false, false, true);
  Q4T_CHECK(response.GetString("status") == "incomplete");
  const auto& output = *response.GetArray("output");
  Q4T_CHECK(output.array[0].GetString("status") == "incomplete");
  ResponsesExecution follow(store);
  Q4T_CHECK(Ok(follow.Prepare(Follow(output), Config(), Access(), 44)));
  Q4T_CHECK(follow.Generation().input.items.array[1].GetString("status") ==
            "incomplete");
  return true;
}

Q4T_TEST(responses_crypto_restored_context_is_measured_and_truncated_normally) {
  Fixture f;
  ObjectStore store(f.options);
  const auto response = Generated(store);
  auto input = *response.GetArray("output");
  input.array[0] = OpaqueOnly(input.array[0]);
  input.array.push_back(Parse(R"({"role":"user","content":"new task"})"));
  auto request = Follow(input);
  String(&request, "truncation", "auto");
  ResponsesExecution follow(store);
  Q4T_CHECK(Ok(follow.Prepare(request, Config(), Access(), 44)));
  size_t checks = 0;
  Q4T_CHECK(Ok(follow.FitContext(Config(), [&](const auto& plan, auto* error) {
    ++checks;
    if (plan.input.items.array.size() > 1) {
      *error = {"context_length_exceeded", "messages", 400};
      return q4t::Status::Fail("fixture measured context full");
    }
    return q4t::Status();
  })));
  Q4T_CHECK(checks > 1 && follow.Generation().input.items.array.size() == 1);
  return true;
}

#ifdef Q4T_GRAMMAR_PROCESS_TESTS
Q4T_TEST(responses_crypto_seal_failure_never_publishes_success_terminal) {
  Fixture f;
  ObjectStore store(f.options);
  auto request = Request();
  Field(&request, "store") = Parse("true");
  ResponsesExecution run(store);
  Q4T_CHECK(Ok(run.Prepare(request, Config(), Access(), 42)));
  std::string wire;
  Q4T_CHECK(Ok(run.Begin(&wire)));
  Q4T_CHECK(run.Append(Text("Reasoning", true), "null", &wire));
  Q4T_CHECK(run.Append(Text("42"), "null", &wire));
  struct Reset {
    ~Reset() { SetCryptoUnavailableForTesting(false); }
  } reset;
  SetCryptoUnavailableForTesting(true);
  const auto failure = run.Finish(false, 0, {2, 0, 2, 1}, 43, &wire);
  Q4T_CHECK(failure.code == "state_crypto_unavailable" && wire.empty() &&
            !run.Output().Terminal() && !run.Finished());
  ResponsesExecution::ObserveError(&run, 503, failure.message, failure.code);
  Q4T_CHECK(Ok(run.CloseUnfinished()));
  const auto saved = ResponsesState(store).ReadRecord(run.Id(), Access());
  Q4T_CHECK(Ok(saved) &&
            saved.value.Find("response")->GetString("status") == "failed");
  Q4T_CHECK(OutputJson(saved.value).find("response.completed") ==
            std::string::npos);
  return true;
}
#endif
