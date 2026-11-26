#include "q4t/server/responses_state.h"

#include <sys/stat.h>

#include <cstdlib>
#include <filesystem>

#include "../src/server/chatkit_retry.h"
#include "../src/server/object_protocol.h"
#include "q4t/server/output_schema.h"
#include "q4t/server/responses_execution.h"
#include "q4t/server/responses_api.h"
#include "q4t/server/responses_item_shape.h"
#include "q4t/server/responses_output.h"
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

Q4T_TEST(responses_state_chatkit_cancelled_context_is_bound_to_original_thread) {
  Fixture f;
  ObjectStore objects(f.options);
  ResponsesState states(objects);
  ResponseStatePlan first, second;
  Q4T_CHECK(Ok(states.Resolve(Envelope(R"({"input":"first"})"),
                              Access(), &first)));
  first.chatkit_thread_id = "cthr_reader";
  Q4T_CHECK(Ok(states.Begin(Snapshot(first, "in_progress"), Access(), &first)));
  auto output = Answer();
  Set(&output.array.front(), "status", String("incomplete"));
  auto blank = output.array.front();
  Set(&blank, "id", String("msg_blank"));
  Set(&blank, "content", Parse(R"([{"type":"output_text","text":"  ",
                                    "annotations":[]}])"));
  output.array.push_back(std::move(blank));
  Q4T_CHECK(Ok(states.Finish(Snapshot(first, "cancelled", output),
                             Parse("[]"), Access(), &first)));
  auto next = Envelope(R"({"input":"next"})");
  next.previous_response_id = first.id;
  Q4T_CHECK(!states.Resolve(next, Access(), &second, nullptr, {},
                            "cthr_other").ok());
  Q4T_CHECK(states.Resolve(next, Access("client:b"), &second, nullptr, {},
                           "cthr_reader").http_status == 404);
  Q4T_CHECK(Ok(states.Resolve(next, Access(), &second, nullptr, {},
                              "cthr_reader")));
  const auto& history = second.history.array;
  Q4T_CHECK(history.size() == 4 && history[1].GetString("id") == "msg_answer");
  Q4T_CHECK(history[2].GetString("role") == "developer" &&
            history[3].GetString("role") == "user");
  Q4T_CHECK(OutputJson(history[2]).find("user cancelled") != std::string::npos);
  Q4T_CHECK(Ok(states.Resolve(next, Access(), &second)));
  Q4T_CHECK(second.own_input.array.size() == 1 &&
            second.history.array[2].GetString("id") == "msg_blank");
  return true;
}

Q4T_TEST(chatkit_retry_replays_unpublished_input_from_cancelled_turn) {
  Fixture f;
  ObjectStore objects(f.options);
  ResponsesState state(objects);
  ResponseStatePlan first, later;
  Q4T_CHECK(Ok(state.Resolve(Envelope(R"({"input":"first"})"),
                             Access(), &first)));
  first.chatkit_thread_id = "cthr_reader";
  Q4T_CHECK(Ok(state.Begin(Snapshot(first, "in_progress"), Access(), &first)));
  Q4T_CHECK(Ok(state.Finish(Snapshot(first, "cancelled", Answer()),
                            Parse("[]"), Access(), &first)));
  auto next = Envelope(R"({"input":"later"})");
  next.previous_response_id = first.id;
  Q4T_CHECK(Ok(state.Resolve(next, Access(), &later, nullptr, {},
                             first.chatkit_thread_id)));
  later.chatkit_thread_id = first.chatkit_thread_id;
  Q4T_CHECK(Ok(state.Begin(Snapshot(later, "in_progress"), Access(), &later)));
  Q4T_CHECK(Ok(state.Finish(Snapshot(later, "completed"), Parse("[]"),
                            Access(), &later)));

  const auto& original = first.own_input.array.front();
  auto request = Parse(R"({"background":true,"store":true,
                          "input":[{"type":"item_reference"}]})");
  Set(&object_detail::Field(request, "input").array.front(), "id", *original.Find("id"));
  const auto& input = *request.Find("input");
  Q4T_CHECK(state.ResolveItems(input, Access()).http_status == 404);
  ChatPlanConfig config;
  config.model = "cpu";
  for (const auto* source : {&first, &later}) {
    auto thread = Parse(R"({"thread":{"id":"cthr_reader"},"items":[
      {"type":"chatkit.user_message","id":"item_visible","attachments":[],
       "content":[{"type":"input_text","text":"first"}]}]})");
    if (source == &later)
      object_detail::Field(thread, "items").array.push_back(Parse(R"({
        "type":"chatkit.user_message","id":"item_later","attachments":[],
        "content":[{"type":"input_text","text":"later"}]})"));
    Set(&thread, "last_response", String(source->id));
    auto saved = state.ReadRecord(source->id, Access());
    Q4T_CHECK(Ok(saved));
    chatkit_detail::RetryHistory prefix;
    Json user;
    Q4T_CHECK(Ok(chatkit_detail::SelectRetryHistory(
        saved.value, "item_visible", &thread, &user, &prefix)));
    ResponsesExecution retry(objects);
    Q4T_CHECK(Ok(retry.Prepare(request, config, Access(), 42, true, nullptr,
                               nullptr, &prefix, first.chatkit_thread_id)));
    Json frozen;
    Q4T_CHECK(Ok(retry.FreezeInputs(&frozen)));
    Q4T_CHECK(OutputJson(*frozen.Find("history")) == OutputJson(first.history));
    Q4T_CHECK(OutputJson(*frozen.Find("own_input")) ==
              OutputJson(first.own_input));
    auto wrong = request;
    Set(&object_detail::Field(wrong, "input").array.front(), "id",
        String("item_never_admitted"));
    ResponsesExecution unrelated(objects);
    Q4T_CHECK(!unrelated.Prepare(wrong, config, Access(), 42, true, nullptr,
                                 nullptr, &prefix, first.chatkit_thread_id)
                   .ok());
    auto altered = prefix;
    Set(&altered.items.array.back(), "content",
        Parse(R"([{"type":"input_text","text":"changed"}])"));
    ResponsesExecution mismatch(objects);
    Q4T_CHECK(!mismatch.Prepare(request, config, Access(), 42, true, nullptr,
                                nullptr, &altered, first.chatkit_thread_id)
                   .ok());
    ResponsesExecution foreign(objects), sibling(objects);
    Q4T_CHECK(foreign.Prepare(request, config, Access("client:b"), 42, true,
                             nullptr, nullptr, &prefix, first.chatkit_thread_id)
                  .http_status == 404);
    Q4T_CHECK(!sibling.Prepare(request, config, Access(), 42, true, nullptr,
                               nullptr, &prefix, "cthr_other").ok());
  }
  Q4T_CHECK(state.ResolveItems(input, Access()).http_status == 404);
  return true;
}

Q4T_TEST(responses_state_tracks_chatkit_item_times_in_private_journal_only) {
  Fixture f;
  ObjectStore objects(f.options);
  ResponsesState state(objects);
  for (const bool chatkit : {false, true}) {
    ResponseStatePlan plan;
    Q4T_CHECK(
        Ok(state.Resolve(Envelope(R"({"input":"hello"})"), Access(), &plan)));
    if (chatkit) plan.chatkit_thread_id = "cthr_times";
    Q4T_CHECK(Ok(state.Begin(Snapshot(plan, "in_progress"), Access(), &plan)));
    const auto output = Answer();
    Q4T_CHECK(Ok(state.Checkpoint(Snapshot(plan, "in_progress", output),
                                  Parse("[]"), Access(), &plan)));
    Json times;
    auto before = state.Retrieve(plan.id, Access(), &times);
    Q4T_CHECK(Ok(before) && !before.value.Has("chatkit_item_times"));
    if (chatkit) {
      Q4T_CHECK(times.GetInt("msg_answer") > 0);
    } else {
      Q4T_CHECK(times.IsNull());
    }
    Q4T_CHECK(Ok(state.Finish(Snapshot(plan, "completed", output), Parse("[]"),
                              Access(), &plan)));
    Json saved;
    auto after = state.Retrieve(plan.id, Access(), &saved);
    Q4T_CHECK(Ok(after) && !after.value.Has("chatkit_item_times") &&
              OutputJson(saved) == OutputJson(times));
    Q4T_CHECK(state.Retrieve(plan.id, Access("client:b"), &saved).http_status ==
              404);
  }
  return true;
}

Q4T_TEST(refusal_state_persistence_restart_history_and_client_isolation) {
  Fixture f;
  std::string id, original;
  {
    ObjectStore objects(f.options);
    ResponsesState states(objects);
    const auto envelope = Envelope(R"({"input":"private query"})");
    ResponseStatePlan plan;
    Q4T_CHECK(Ok(states.Resolve(envelope, Access(), &plan)));
    id = plan.id;
    ResponsesOutput output;
    std::string wire, part;
    Q4T_CHECK(
        output.Init(Parse(R"({"model":"cpu","store":true})"), id, 42, false));
    Q4T_CHECK(output.Start(&wire));
    Q4T_CHECK(Ok(states.Begin(output.Snapshot(), Access(), &plan)));
    const auto request = Parse(R"({"messages":[{"role":"user",
        "content":"private query"}],"enable_thinking":false})");
    ChatResponseParser parser;
    std::vector<ResponseDelta> deltas;
    Q4T_CHECK(parser.Init(request, "<think>\n\n</think>\n\n", "r", true));
    Q4T_CHECK(parser.Append("<q4t_refusal>不能提供</q4t_refusal>", &deltas));
    Q4T_CHECK(parser.Finish(false, &deltas));
    for (const auto& delta : deltas)
      Q4T_CHECK(output.Append(delta, "null", &part));
    Q4T_CHECK(output.Finish(false, 0, {10, 0, 5, 0, 0}, 43, &part));
    original = output.Json();
    Q4T_CHECK(
        Ok(states.Finish(output.Snapshot(), Parse("[]"), Access(), &plan)));
  }
  ObjectStore reopened(f.options);
  ResponsesState states(reopened);
  const auto saved = states.Retrieve(id, Access());
  Q4T_CHECK(Ok(saved) && OutputJson(saved.value) == original);
  Q4T_CHECK(states.Retrieve(id, Access("client:b")).http_status == 404);
  auto followup = Envelope(R"({"input":"then use public records"})");
  followup.previous_response_id = id;
  ResponseStatePlan next;
  Q4T_CHECK(Ok(states.Resolve(followup, Access(), &next)));
  Q4T_CHECK(next.history.array.size() == 3);
  Q4T_CHECK(next.history.array[1].GetArray("content")->array[0].GetString(
                "refusal") == "不能提供");
  ResponsesGenerationPlan generation;
  ChatPlanConfig config;
  config.model = "cpu";
  ChatPlanError error;
  Q4T_CHECK(PrepareResponsesGeneration(followup, next.history, config,
                                       &generation, &error));
  Q4T_CHECK(OutputJson(generation.input.messages).find("不能提供") !=
            std::string::npos);
  Q4T_CHECK(!generation.input.messages.array[1].Has("tool_calls"));
  return true;
}

Q4T_TEST(responses_state_interruption_preserves_committed_agent_items) {
  Fixture f;
  ObjectStore objects(f.options);
  ResponsesState state(objects);
  ResponseStatePlan plan;
  Q4T_CHECK(Ok(state.Resolve(Envelope(R"({"input":"delegate"})"), Access(),
                             &plan)));
  plan.multi_agent_beta = true;
  auto output = Parse(R"([
    {"type":"multi_agent_call","id":"agentcall_1","call_id":"call_1",
     "action":"spawn_agent","agent":{"agent_name":"root"},"arguments":"{}"},
    {"type":"multi_agent_call_output","id":"agentresult_1","call_id":"call_1",
     "action":"spawn_agent","agent":{"agent_name":"root"},
     "output":[{"type":"output_text","text":"queued","annotations":[]}]}])");
  ChatPlanError error;
  for (const auto& item : output.array) {
    Q4T_CHECK(ValidateResponsesItemShape(item, ResponsesItemShape::kOutput,
                                         &error, "output", true).ok());
    auto invalid = item;
    Set(&invalid, "status", String("incomplete"));
    Q4T_CHECK(!ValidateResponsesItemShape(invalid, ResponsesItemShape::kOutput,
                                          &error, "output", true).ok());
  }
  auto message = Answer().array.front();
  output.array.push_back(message);
  Set(&message, "id", String("msg_next"));
  Set(&message, "status", String("in_progress"));
  output.array.push_back(message);
  output.array.push_back(Parse(R"({"type":"image_generation_call",
    "id":"img_1","result":null,"status":"generating"})"));
  output.array.push_back(Parse(R"({"type":"apply_patch_call","id":"patch_1",
    "call_id":"call_2","status":"in_progress",
    "operation":{"type":"delete_file","path":"sample.txt"}})"));
  Q4T_CHECK(Ok(state.Begin(Snapshot(plan, "in_progress"), Access(), &plan)));
  Q4T_CHECK(Ok(state.Checkpoint(Snapshot(plan, "in_progress", output),
                                Parse("[]"), Access(), &plan)));
  const auto closed =
      state.Interrupt(plan.id, Access(), "background_execution_interrupted",
                      "Previous execution process exited");
  Q4T_CHECK(Ok(closed));
  Q4T_CHECK(closed.value.GetString("status") == "failed");
  const auto* saved = closed.value.GetArray("output");
  Q4T_CHECK(saved && saved->array.size() == 6);
  Q4T_CHECK(!saved->array[0].Find("status") && !saved->array[1].Find("status"));
  Q4T_CHECK(saved->array[2].GetString("status") == "completed");
  Q4T_CHECK(saved->array[3].GetString("status") == "incomplete");
  Q4T_CHECK(saved->array[4].GetString("status") == "failed");
  Q4T_CHECK(saved->array[5].GetString("status") == "in_progress");
  return true;
}

Q4T_TEST(responses_legacy_failure_projects_get_and_resumed_event) {
  Fixture f;
  ObjectStore objects(f.options);
  ResponsesState state(objects);
  ResponseStatePlan plan;
  Q4T_CHECK(Ok(state.Resolve(Envelope(R"({"input":"hi"})"), Access(), &plan)));
  auto started = Snapshot(plan, "in_progress");
  Q4T_CHECK(Ok(state.Begin(started, Access(), &plan)));
  auto failed = Snapshot(plan, "failed");
  Set(&failed, "error", Parse(R"({"code":"background_execution_interrupted",
                                 "message":"Previous execution process exited"})"));
  auto events = Parse(R"([{"type":"response.created","sequence_number":0},
                          {"type":"response.failed","sequence_number":1}])");
  Set(&events.array[0], "response", started);
  Set(&events.array[1], "response", failed);
  Q4T_CHECK(Ok(state.Finish(failed, events, Access(), &plan)));
  const auto before = state.ReadRecord(plan.id, Access());
  Q4T_CHECK(Ok(before));
  ResponsesReadPlan read;
  ResponsesReplay replay;
  const auto path = "/v1/responses/" + plan.id;
  Q4T_CHECK(Ok(PlanResponsesRead("GET", path, "", &read)));
  const auto result = ReadResponsesState(objects, read, Access(), &replay);
  Q4T_CHECK(Ok(result));
  const auto* error = result.value.Find("error");
  Q4T_CHECK(error && error->GetString("code") == "server_error");
  Q4T_CHECK(error->GetString("type") == "background_execution_interrupted" &&
            error->GetString("message") == "Previous execution process exited");
  Q4T_CHECK(Ok(PlanResponsesRead("GET", path + "?stream=true&starting_after=0",
                                 "", &read)));
  Q4T_CHECK(Ok(ReadResponsesState(objects, read, Access(), &replay)));
  Set(&events.array[1], "response", result.value);
  Q4T_CHECK(replay.terminal && replay.last_sequence == 1 &&
            replay.wire == "event: response.failed\r\ndata: " +
                               OutputJson(events.array[1]) + "\r\n\r\n");
  Q4T_CHECK(ReadResponsesState(objects, read, Access("client:b"), &replay)
                    .http_status == 404 &&
            replay.wire.empty());
  Q4T_CHECK(OutputJson(state.ReadRecord(plan.id, Access()).value) ==
            OutputJson(before.value));
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

Q4T_TEST(responses_state_large_history_survives_checkpoint_and_previous) {
  Fixture fixture;
  const std::string bytes(17ULL << 20, 'A');
  auto envelope = Envelope(R"({"input":[{"role":"user","content":[
    {"type":"input_file","filename":"original.txt","file_data":"AA=="}]}]})");
  auto input = *envelope.request.GetArray("input");
  auto content = *input.array[0].GetArray("content");
  Set(&content.array[0], "file_data", String(bytes));
  Set(&input.array[0], "content", content);
  Set(&envelope.request, "input", input);
  std::string id;
  {
    ObjectStore objects(fixture.options);
    ResponsesState states(objects);
    auto access = Access();
    access.deadline = Clock::now() + 30s;
    ResponseStatePlan plan;
    Q4T_CHECK(Ok(states.Resolve(envelope, access, &plan)));
    id = plan.id;
    Q4T_CHECK(Ok(states.Begin(Snapshot(plan, "in_progress"), access, &plan)));
    Q4T_CHECK(Ok(states.Checkpoint(Snapshot(plan, "in_progress"), Parse("[]"),
                                   access, &plan)));
    Q4T_CHECK(Ok(states.Finish(Snapshot(plan, "completed", Answer()),
                               Parse("[]"), access, &plan)));
  }
  ObjectStore objects(fixture.options);
  ResponsesState states(objects);
  auto access = Access();
  access.deadline = Clock::now() + 30s;
  ResponseStatePlan loaded, next;
  Q4T_CHECK(Ok(states.LoadPlan(id, access, &loaded)));
  Q4T_CHECK(loaded.own_input.array[0].GetArray("content")->array[0].GetString(
                "file_data") == bytes);
  auto followup = Envelope(R"({"input":"next"})");
  followup.previous_response_id = id;
  Q4T_CHECK(Ok(states.Resolve(followup, access, &next)));
  Q4T_CHECK(next.history.array.size() == 3 &&
            next.history.array[0].GetArray("content")->array[0].GetString(
                "file_data") == bytes);
  Q4T_CHECK(OutputJson(next.history).find("_q4t_application_payload") ==
            std::string::npos);
  return true;
}
