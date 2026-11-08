#include "q4t/server/responses_state.h"

#include <sys/stat.h>

#include <cstdlib>
#include <filesystem>

#include "q4t/server/output_schema.h"
#include "q4t/test.h"

namespace {
using namespace q4t::server;
using q4t::io::Json;
using Clock = std::chrono::steady_clock;
using namespace std::chrono_literals;
namespace fs = std::filesystem;
Json Parse(const std::string& text) {
  Json value;
  if (!q4t::io::ParseJson(text, &value, true))
    throw std::runtime_error("fixture JSON");
  return value;
}
void Set(Json* value, std::string name, Json item) {
  for (auto& [key, current] : value->object) {
    if (key == name) {
      current = std::move(item);
      return;
    }
  }
  value->object.emplace_back(std::move(name), std::move(item));
}
Json String(std::string text) {
  Json value;
  value.type = Json::Type::kString;
  value.str = std::move(text);
  return value;
}
struct Fixture {
  fs::path directory;
  ObjectStoreOptions options;
  Fixture() {
    const auto* output = std::getenv("Q4T_TEST_OUTPUT_DIR");
    auto root =
        fs::absolute(output ? output : ".q4t-work/responses-state-host");
    fs::create_directories(root);
    auto name = (root / "response-state-XXXXXX").string();
    if (!mkdtemp(name.data())) throw std::runtime_error("fixture directory");
    directory = name;
    options.directory = (directory / "store").string();
    options.restart_backoff_ms = 0;
  }
};
StateAccess Access(std::string scope = "client:a") {
  return {std::move(scope), Clock::now() + 5s, {}};
}
ResponsesEnvelope Envelope(const std::string& text) {
  ResponsesEnvelope envelope;
  ChatPlanConfig config;
  config.model = "cpu";
  ChatPlanError error;
  auto status = ParseResponsesEnvelope(Parse(text), config, &envelope, &error);
  if (!status) throw std::runtime_error(error.param + ": " + status.message());
  return envelope;
}
bool Ok(const ObjectReply& reply) {
  if (!reply.ok())
    std::fprintf(stderr, "%s: %s\n", reply.code.c_str(), reply.message.c_str());
  return reply.ok();
}
Json Snapshot(const ResponseStatePlan& plan, std::string status,
              Json output = Parse("[]")) {
  auto value = Parse(R"({"object":"response","model":"cpu"})");
  Set(&value, "id", String(plan.id));
  Set(&value, "status", String(std::move(status)));
  Set(&value, "output", std::move(output));
  return value;
}
Json Answer() {
  return Parse(R"([{"id":"msg_answer","type":"message","role":"assistant",
    "status":"completed","content":[{"type":"output_text","text":"42","annotations":[]}]}])");
}
ObjectRequest Conversation() {
  ObjectRequest request;
  request.operation = ObjectOperation::kPut;
  request.kind = ObjectKind::kConversation;
  request.scope = "client:a";
  request.id = "conv_1";
  request.ttl_seconds = 0;
  request.data = Parse(R"({"resource":{"id":"conv_1","object":"conversation",
    "created_at":1,"metadata":{}},"items":[{"id":"msg_history","type":"message",
    "role":"user","content":[{"type":"input_text","text":"remember 42"}]}]})");
  return request;
}
ObjectReply ReadConversation(ObjectStore& store) {
  auto request = Conversation();
  request.operation = ObjectOperation::kGet;
  return store.Execute(request, Clock::now() + 5s);
}
}  // namespace

Q4T_TEST(responses_state_stateless_path_does_not_touch_storage) {
  Fixture f;
  f.options.directory = "/invalid/unconfigured/never-open";
  ObjectStore objects(f.options);
  ResponsesState states(objects);
  ResponseStatePlan plan;
  Q4T_CHECK(Ok(states.Resolve(Envelope(R"({"input":"hello","store":false})"),
                              Access(), &plan)));
  Q4T_CHECK(Ok(states.Begin(Snapshot(plan, "in_progress"), Access(), &plan)));
  Q4T_CHECK(Ok(states.Finish(Snapshot(plan, "completed", Answer()), Parse("[]"),
                             Access(), &plan)));
  Q4T_CHECK(objects.Health().workers_started == 0 && plan.finished);
  Q4T_CHECK(plan.own_input.array[0].GetArray("content") != nullptr);
  return true;
}

Q4T_TEST(responses_state_checkpoints_require_scope_and_current_revision) {
  Fixture f;
  ObjectStore objects(f.options);
  ResponsesState state(objects);
  ResponseStatePlan plan;
  Q4T_CHECK(
      Ok(state.Resolve(Envelope(R"({"input":"hello"})"), Access(), &plan)));
  const auto snapshot = Snapshot(plan, "in_progress");
  Q4T_CHECK(Ok(state.Begin(snapshot, Access(), &plan)));
  auto stale = plan;
  Q4T_CHECK(Ok(state.Checkpoint(snapshot, Parse("[]"), Access(), &plan)));
  Q4T_CHECK(plan.response_revision == stale.response_revision + 1);
  Q4T_CHECK(
      state.Checkpoint(snapshot, Parse("[]"), Access(), &stale).http_status ==
      409);
  Q4T_CHECK(
      state.Checkpoint(snapshot, Parse("[]"), Access("client:b"), &plan).code ==
      "object_scope_mismatch");
  Q4T_CHECK(
      state
          .Checkpoint(Snapshot(plan, "completed"), Parse("[]"), Access(), &plan)
          .code == "response_state_invalid");
  Q4T_CHECK(
      Ok(state.Finish(Snapshot(plan, "failed"), Parse("[]"), Access(), &plan)));
  Q4T_CHECK(!state.Checkpoint(snapshot, Parse("[]"), Access(), &plan).ok());
  Q4T_CHECK(state.Retrieve(plan.id, Access()).value.GetString("status") ==
            "failed");
  return true;
}

Q4T_TEST(
    responses_state_terminal_reserve_cannot_publish_success_or_cross_scope) {
  Fixture f;
  ObjectStore objects(f.options);
  ResponsesState state(objects);
  ResponseStatePlan plan;
  Q4T_CHECK(
      Ok(state.Resolve(Envelope(R"({"input":"hello"})"), Access(), &plan)));
  Q4T_CHECK(Ok(state.Begin(Snapshot(plan, "in_progress"), Access(), &plan)));
  Q4T_CHECK(
      !state.FailWithinReservation(Snapshot(plan, "completed"), Access(), &plan)
           .ok());
  Q4T_CHECK(
      !state
           .FailWithinReservation(Snapshot(plan, "incomplete"), Access(), &plan)
           .ok());
  Q4T_CHECK(state
                .FailWithinReservation(Snapshot(plan, "failed"),
                                       Access("client:b"), &plan)
                .code == "object_scope_mismatch");
  Q4T_CHECK(Ok(state.FailWithinReservation(Snapshot(plan, "cancelled"),
                                           Access(), &plan)));
  const auto saved = state.ReadRecord(plan.id, Access());
  Q4T_CHECK(Ok(saved) && plan.finished);
  Q4T_CHECK(saved.value.Find("response")->GetString("status") == "cancelled");
  Q4T_CHECK(saved.value.Find("response")->Find("error")->IsNull());
  Q4T_CHECK(!saved.value.Has("terminal_reserve"));
  Q4T_CHECK(saved.value.GetArray("events")->array.empty());
  return true;
}

Q4T_TEST(
    responses_state_item_identity_preserves_exact_numbers_and_array_order) {
  Q4T_CHECK(Ok(CompareStoredItem(
      Parse(R"({"n":1,"nested":{"x":[true,null,"a"],"y":2}})"),
      Parse(R"({"nested":{"y":2.0,"x":[true,null,"a"]},"n":1e0})"))));
  Q4T_CHECK(CompareStoredItem(Parse("[1,2]"), Parse("[2,1]")).code ==
            "item_identity_conflict");
  Q4T_CHECK(
      CompareStoredItem(Parse("9007199254740992"), Parse("9007199254740993"))
          .code == "item_identity_conflict");
  Q4T_CHECK(CompareStoredItem(Parse("1"), Parse("\"1\"")).code ==
            "item_identity_conflict");
  return true;
}

Q4T_TEST(responses_state_previous_history_does_not_inherit_instructions) {
  Fixture f;
  ObjectStore objects(f.options);
  ResponsesState states(objects);
  ResponseStatePlan first, second;
  Q4T_CHECK(Ok(states.Resolve(
      Envelope(R"({"input":"first","instructions":"OLD PRIVATE RULE"})"),
      Access(), &first)));
  Q4T_CHECK(Ok(states.Begin(Snapshot(first, "in_progress"), Access(), &first)));
  Q4T_CHECK(Ok(states.Finish(Snapshot(first, "completed", Answer()),
                             Parse("[]"), Access(), &first)));
  auto next = Envelope(R"({"input":"next","instructions":"NEW RULE"})");
  next.previous_response_id = first.id;
  Q4T_CHECK(Ok(states.Resolve(next, Access(), &second)));
  Q4T_CHECK(second.history.array.size() == 3 &&
            second.own_input.array.size() == 1);
  Q4T_CHECK(OutputJson(second.history).find("OLD PRIVATE RULE") ==
            std::string::npos);
  ResponsesGenerationPlan generation;
  ChatPlanConfig config;
  config.model = "cpu";
  ChatPlanError error;
  Q4T_CHECK(PrepareResponsesGeneration(next, second.history, config,
                                       &generation, &error));
  Q4T_CHECK(OutputJson(generation.input.messages).find("NEW RULE") !=
            std::string::npos);
  Q4T_CHECK(states.Retrieve(first.id, Access("client:b")).http_status == 404);
  return true;
}

Q4T_TEST(responses_state_running_terminal_and_delete_lifecycle) {
  Fixture f;
  ObjectStore objects(f.options);
  ResponsesState states(objects);
  ResponseStatePlan plan, next;
  Q4T_CHECK(Ok(states.Resolve(Envelope(R"({"input":"hi"})"), Access(), &plan)));
  Q4T_CHECK(Ok(states.Begin(Snapshot(plan, "queued"), Access(), &plan)));
  Q4T_CHECK(states.Retrieve(plan.id, Access()).value.GetString("status") ==
            "queued");
  auto envelope = Envelope(R"({"input":"next"})");
  envelope.previous_response_id = plan.id;
  Q4T_CHECK(states.Resolve(envelope, Access(), &next).http_status == 409);
  Q4T_CHECK(states.Erase(plan.id, Access()).http_status == 409);
  Q4T_CHECK(Ok(states.Finish(Snapshot(plan, "completed", Answer()), Parse("[]"),
                             Access(), &plan)));
  Q4T_CHECK(states.Retrieve(plan.id, Access()).value.GetString("status") ==
            "completed");
  Q4T_CHECK(!states
                 .Finish(Snapshot(plan, "completed", Answer()), Parse("[]"),
                         Access(), &plan)
                 .ok());
  Q4T_CHECK(Ok(states.Erase(plan.id, Access())));
  Q4T_CHECK(states.Resolve(envelope, Access(), &next).http_status == 404);
  return true;
}

Q4T_TEST(responses_state_item_reference_requires_client_ownership) {
  Fixture f;
  ObjectStore objects(f.options);
  ResponsesState states(objects);
  ResponseStatePlan plan, reference;
  Q4T_CHECK(Ok(states.Resolve(Envelope(R"({"input":"hi"})"), Access(), &plan)));
  Q4T_CHECK(Ok(states.Begin(Snapshot(plan, "in_progress"), Access(), &plan)));
  Q4T_CHECK(Ok(states.Finish(Snapshot(plan, "completed", Answer()), Parse("[]"),
                             Access(), &plan)));
  auto request = Envelope(
      R"({"store":false,"input":[{"type":"item_reference","id":"msg_answer"}]})");
  Q4T_CHECK(Ok(states.Resolve(request, Access(), &reference)));
  Q4T_CHECK(reference.history.array[0].GetString("type") == "message");
  Q4T_CHECK(
      states.Resolve(request, Access("client:b"), &reference).http_status ==
      404);
  return true;
}

Q4T_TEST(responses_state_stateless_conversation_still_appends_atomically) {
  Fixture f;
  ObjectStore objects(f.options);
  Q4T_CHECK(Ok(objects.Execute(Conversation(), Clock::now() + 5s)));
  ResponsesState states(objects);
  ResponseStatePlan plan;
  Q4T_CHECK(Ok(states.Resolve(
      Envelope(R"({"input":"continue","store":false,"conversation":"conv_1"})"),
      Access(), &plan)));
  Q4T_CHECK(plan.history.array.size() == 2);
  Q4T_CHECK(Ok(states.Begin(Snapshot(plan, "in_progress"), Access(), &plan)));
  Q4T_CHECK(Ok(states.Finish(Snapshot(plan, "completed", Answer()), Parse("[]"),
                             Access(), &plan)));
  const auto conversation = ReadConversation(objects);
  Q4T_CHECK(Ok(conversation) && conversation.value.GetInt("revision") == 2);
  Q4T_CHECK(conversation.value.Find("data")->GetArray("items")->array.size() ==
            3);
  Q4T_CHECK(states.Retrieve(plan.id, Access()).http_status == 404);
  ObjectRequest item;
  item.scope = "client:a";
  item.kind = ObjectKind::kConversationItem;
  item.id = "msg_answer";
  Q4T_CHECK(
      objects.Execute(item, Clock::now() + 5s).value.GetInt("expires_at", -1) ==
      0);
  return true;
}

Q4T_TEST(responses_state_conversation_conflict_cannot_publish_completed) {
  Fixture f;
  ObjectStore objects(f.options);
  Q4T_CHECK(Ok(objects.Execute(Conversation(), Clock::now() + 5s)));
  ResponsesState states(objects);
  ResponseStatePlan plan;
  Q4T_CHECK(Ok(states.Resolve(
      Envelope(R"({"input":"continue","conversation":"conv_1"})"), Access(),
      &plan)));
  Q4T_CHECK(Ok(states.Begin(Snapshot(plan, "in_progress"), Access(), &plan)));
  auto concurrent = Conversation();
  concurrent.expected_revision = 1;
  Q4T_CHECK(Ok(objects.Execute(concurrent, Clock::now() + 5s)));
  Q4T_CHECK(states
                .Finish(Snapshot(plan, "completed", Answer()), Parse("[]"),
                        Access(), &plan)
                .http_status == 409);
  Q4T_CHECK(states.Retrieve(plan.id, Access()).value.GetString("status") ==
            "in_progress");
  Q4T_CHECK(Ok(
      states.Finish(Snapshot(plan, "failed"), Parse("[]"), Access(), &plan)));
  Q4T_CHECK(states.Retrieve(plan.id, Access()).value.GetString("status") ==
            "failed");
  Q4T_CHECK(ReadConversation(objects)
                .value.Find("data")
                ->GetArray("items")
                ->array.size() == 1);
  return true;
}

Q4T_TEST(responses_state_tool_result_resolves_original_call_identity) {
  Fixture f;
  ObjectStore objects(f.options);
  ResponsesState states(objects);
  ResponseStatePlan first, next;
  Q4T_CHECK(
      Ok(states.Resolve(Envelope(R"({"input":"lookup"})"), Access(), &first)));
  Q4T_CHECK(Ok(states.Begin(Snapshot(first, "in_progress"), Access(), &first)));
  auto call =
      Parse(R"([{"id":"fc_lookup","type":"function_call","call_id":"call_doc",
    "name":"lookup","arguments":"{\"path\":\"README.md\"}","status":"completed"}])");
  Q4T_CHECK(Ok(states.Finish(Snapshot(first, "completed", call), Parse("[]"),
                             Access(), &first)));
  auto reply = Envelope(
      R"({"input":[{"type":"function_call_output","call_id":"call_doc","output":"title"}]})");
  reply.previous_response_id = first.id;
  Q4T_CHECK(Ok(states.Resolve(reply, Access(), &next)));
  auto request = reply.request;
  Set(&request, "input", next.history);
  ResponsesInputPlan normalized;
  ChatPlanError error;
  Q4T_CHECK(NormalizeResponsesInput(request, &normalized, &error));
  Q4T_CHECK(OutputJson(normalized.messages).find("call_doc") !=
            std::string::npos);
  return true;
}

Q4T_TEST(responses_state_prepared_plan_cannot_change_client) {
  Fixture f;
  ObjectStore objects(f.options);
  ResponsesState states(objects);
  ResponseStatePlan plan;
  Q4T_CHECK(
      Ok(states.Resolve(Envelope(R"({"input":"private"})"), Access(), &plan)));
  Q4T_CHECK(
      states.Begin(Snapshot(plan, "in_progress"), Access("client:b"), &plan)
          .http_status == 403);
  Q4T_CHECK(objects.Health().workers_started == 0);
  Q4T_CHECK(Ok(states.Begin(Snapshot(plan, "in_progress"), Access(), &plan)));
  Q4T_CHECK(states
                .Finish(Snapshot(plan, "completed", Answer()), Parse("[]"),
                        Access("client:b"), &plan)
                .http_status == 403);
  Q4T_CHECK(states.Retrieve(plan.id, Access("client:b")).http_status == 404);
  Q4T_CHECK(Ok(states.Finish(Snapshot(plan, "completed", Answer()), Parse("[]"),
                             Access(), &plan)));
  return true;
}

Q4T_TEST(responses_state_rejects_malformed_generated_items_before_commit) {
  Fixture f;
  ObjectStore objects(f.options);
  ResponsesState states(objects);
  ResponseStatePlan plan;
  Q4T_CHECK(
      Ok(states.Resolve(Envelope(R"({"input":"hello"})"), Access(), &plan)));
  Q4T_CHECK(Ok(states.Begin(Snapshot(plan, "in_progress"), Access(), &plan)));
  const auto invalid = Parse(R"([{"type":"function_call","id":"fc_bad",
    "call_id":"call_bad","arguments":"{}"}])");
  Q4T_CHECK(states
                .Finish(Snapshot(plan, "completed", invalid), Parse("[]"),
                        Access(), &plan)
                .code == "response_state_invalid");
  Q4T_CHECK(states.Retrieve(plan.id, Access()).value.GetString("status") ==
            "in_progress");
  Q4T_CHECK(Ok(
      states.Finish(Snapshot(plan, "failed"), Parse("[]"), Access(), &plan)));
  return true;
}
