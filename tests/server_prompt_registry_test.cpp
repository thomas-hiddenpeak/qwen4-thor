#include "q4t/server/agent_registration.h"
#include "q4t/server/agent_session.h"
#include "q4t/server/prompt_registry.h"

#include <algorithm>
#include <cstdlib>
#include <set>
#include <filesystem>
#include <fstream>
#include <sstream>
#include <thread>

#include "q4t/server/chat_prompt.h"
#include "q4t/server/http_connection.h"
#include "q4t/server/output_schema.h"
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
    throw std::runtime_error("prompt fixture JSON");
  return value;
}
void Set(Json* object, const std::string& key, Json value) {
  for (auto& [name, field] : object->object)
    if (name == key) {
      field = std::move(value);
      return;
    }
  object->object.emplace_back(key, std::move(value));
}
void SetRequest(Json* definition, const std::string& key, Json value) {
  auto request = *definition->Find("request");
  Set(&request, key, std::move(value));
  Set(definition, "request", std::move(request));
}
bool Ok(const ObjectReply& result) {
  if (!result.ok())
    std::fprintf(stderr, "%s %s %s\n", result.code.c_str(),
                 result.message.c_str(),
                 result.value.GetString("param").c_str());
  return result.ok();
}
StateAccess Access(std::string scope = "client:prompt-a") {
  return {std::move(scope), Clock::now() + 5s, {}};
}
ChatPlanConfig Config() {
  ChatPlanConfig c;
  c.model = "cpu";
  return c;
}
struct Fixture {
  ObjectStoreOptions options;
  Fixture() {
    const char* env = std::getenv("Q4T_TEST_OUTPUT_DIR");
    auto root = std::filesystem::absolute(env ? env : ".q4t-work/prompt-cpu");
    std::filesystem::create_directories(root);
    auto path = (root / "prompts-XXXXXX").string();
    if (!mkdtemp(path.data())) throw std::runtime_error("prompt fixture path");
    options.directory = path + "/objects";
    options.restart_backoff_ms = 0;
  }
};
Json Definition(std::string value = "{{value}}") {
  auto definition =
      Parse(R"({"request":{"instructions":"Answer {{name}} exactly.",
    "input":[{"role":"user","content":""}],"reasoning":{"effort":"none"},
    "temperature":0},"variables":{"name":"string","value":"string"}})");
  auto input = *definition.Find("request")->Find("input");
  Json text;
  text.type = Json::Type::kString;
  text.str = std::move(value);
  Set(&input.array[0], "content", text);
  auto request = *definition.Find("request");
  Set(&request, "input", input);
  Set(&definition, "request", request);
  return definition;
}
Json Request(const std::string& id, std::string extra = "") {
  return Parse(
      "{\"prompt\":{\"id\":\"" + id +
      R"(","variables":{"name":"reader","value":"42"}},"input":"current")" +
      extra + "}");
}
void Record(const Json& value) {
  const char* env = std::getenv("Q4T_TEST_OUTPUT_DIR");
  if (!env) return;
  std::filesystem::create_directories(std::filesystem::path(env) / "fixtures");
  std::ofstream stream(
      std::filesystem::path(env) / "fixtures/prompt-wire.jsonl", std::ios::app);
  stream << OutputJson(value) << '\n';
}
ObjectReply Api(ObjectStore& store, std::string method, std::string path,
                Json body = Json(), StateAccess access = Access()) {
  return HandlePromptRequest(store, method, path,
                             body.IsNull() ? "" : OutputJson(body), Config(),
                             access);
}
}  // namespace

Q4T_TEST(agent_session_wire_shapes_follow_the_sdk_transport_fields) {
  ChatPlanError error;
  for (const char* input : {
           R"({"agent":{"model":"cpu"},"environment":{"type":"none"}})",
           R"({"agent_id":"agent_1","environment":{"type":"none"},
               "stream":true,"input":"hello"})"}) {
    Q4T_CHECK(ValidateAgentSession(Parse(input), AgentSessionShape::kCreate,
                                   &error));
  }
  for (const char* input : {
           R"({"agent_id":"agent_1"})",
           R"({"environment":{"type":"none","container_size":"medium"}})",
           R"({"environment":{"type":"none"},"toolHandlers":{}})",
           R"({"environment":{"type":"none"},"stream":"true"})"}) {
    Q4T_CHECK(!ValidateAgentSession(Parse(input), AgentSessionShape::kCreate,
                                    &error));
  }
  Q4T_CHECK(ValidateAgentSession(
      Parse(R"({"id":"as_1","object":"agent.session.deleted","deleted":true})"),
      AgentSessionShape::kDeleted, &error));
  Q4T_CHECK(!ValidateAgentSession(
      Parse(R"({"id":"as_1","object":"agent.deleted","deleted":true})"),
      AgentSessionShape::kDeleted, &error));
  return true;
}

Q4T_TEST(agent_session_prepares_native_input_and_validates_public_snapshot) {
  Fixture f;
  ObjectStore store(f.options);
  AgentSessionStart start;
  auto begin = [&](const Json& request) {
    return PrepareDirectAgentSession(store, request, Config(), Access(), "session_1",
                                     123, &start);
  };
  auto request = Parse(R"({"agent":{"model":"cpu"},
    "environment":{"type":"none"},"input":"project records",
    "metadata":{"project":"q4t"},"spend_control":{"limit":25}})");
  Q4T_CHECK(Ok(begin(request)));
  Q4T_CHECK(start.session.GetString("object") == "agent.session" &&
            start.session.GetString("status") == "in_progress" &&
            start.session.GetInt("last_active_at") == 123);
  Q4T_CHECK(start.session.Find("agent")->GetString("model") == "cpu");
  Q4T_CHECK(start.session.Find("spend_control")->Find("consumed")->IsNull());
  Q4T_CHECK(start.input.array[0].Find("content")->array[0].GetString("text") ==
            "project records");
  for (const char* invalid : {"null", "[]", R"("")",
                               R"([{"role":"user","content":[]}])"}) {
    auto bad = request;
    Set(&bad, "input", Parse(invalid));
    Q4T_CHECK(begin(bad).http_status == 400);
  }
  Set(&request, "spend_control", Parse(R"({"limit":0})"));
  Q4T_CHECK(begin(request).http_status == 400);
  return true;
}

Q4T_TEST(agent_session_snapshot_owns_settings_and_omits_transport_secrets) {
  Fixture f;
  ObjectStore store(f.options);
  auto source = HandleAgentRegistration(
      store, "POST", "/v1/agents", R"({"model":"cpu","name":"records",
        "instructions":"original","tools":[{"type":"mcp","server_label":"docs",
        "transport":{"type":"http","server_url":"https://example.org",
        "headers":{"x-correlation":"private-config"}}}]})", Config(), Access());
  Q4T_CHECK(Ok(source));
  auto request = Parse(R"({"environment":{"type":"none"},"input":"hello",
                            "agent":{"instructions":"one session"}})");
  Set(&request, "agent_id", *source.value.Find("id"));
  Q4T_CHECK(ResolveSessionAgent(store, request, Config(), Access("client:other"))
                .http_status == 404);
  auto snapshot = ResolveSessionAgent(store, request, Config(), Access());
  Q4T_CHECK(Ok(snapshot) && snapshot.value.GetString("name") == "records" &&
            snapshot.value.GetString("instructions") == "one session");
  Q4T_CHECK(!snapshot.value.Has("metadata") &&
            OutputJson(snapshot.value).find("private-config") == std::string::npos);
  auto saved = HandleAgentRegistration(store, "GET",
      "/v1/agents/" + source.value.GetString("id"), "", Config(), Access());
  Q4T_CHECK(Ok(saved) && saved.value.GetString("instructions") == "original" &&
            OutputJson(saved.value).find("private-config") != std::string::npos);
  auto inline_request = Parse(R"({"environment":{"type":"none"},"input":"hi",
    "agent":{"model":"cpu","tools":[{"type":"mcp","server_label":"docs",
    "transport":{"type":"http","server_url":"https://example.org",
    "authorization":"private-inline"}}]}})");
  ChatPlanError error;
  Q4T_CHECK(ValidateAgentSession(inline_request, AgentSessionShape::kCreate,
                                 &error));
  auto ephemeral = ResolveSessionAgent(store, inline_request, Config(), Access());
  Q4T_CHECK(Ok(ephemeral) && ephemeral.value.GetString("id").starts_with("agent_") &&
            OutputJson(ephemeral.value).find("private-inline") == std::string::npos);
  Q4T_CHECK(ResolveSessionAgent(store, Parse("{}"), Config(), Access())
                .http_status == 400);
  return true;
}

Q4T_TEST(agent_session_turn_shares_queue_receipt_and_reopens_native_output) {
  Fixture f;
  const auto request = Parse(R"({"agent":{"model":"cpu"},
    "environment":{"type":"none"},"input":"what is six times seven?"})");
  std::string id, turn;
  {
    ObjectStore store(f.options);
    ResponsesExecution run(store);
    auto prepared = PrepareAgentSessionExecution(store, request, Config(), Access(),
                                                 std::time(nullptr), run);
    Q4T_CHECK(Ok(prepared));
    id = prepared.value.GetString("id");
    AgentSessionView view;
    Q4T_CHECK(ReadAgentSessionTurn(store, id, "", Access(), &view).http_status == 404);
    run.BindBackgroundControl(std::make_shared<RequestControl>(
        run.Id(), "", Clock::now() + 5s));
    std::string wire;
    Q4T_CHECK(Ok(run.Begin(&wire)));
    Q4T_CHECK(Ok(ReadAgentSessionTurn(store, id, "", Access(), &view)));
    turn = view.turn.GetString("id");
    Q4T_CHECK(view.turn.GetString("status") == "queued" &&
              view.items.array.size() == 1 &&
              view.turn.Find("started_at")->IsNull());
    Q4T_CHECK(Ok(run.StartRunning(&wire)));
    ResponseDelta delta;
    delta.kind = ResponseDelta::Kind::kContent;
    delta.text = "42";
    Q4T_CHECK(run.Append(delta, "null", &wire));
    Q4T_CHECK(Ok(run.Finish(false, 0, {12, 1, 2, 0}, std::time(nullptr), &wire)));
    Q4T_CHECK(ResponsesState(store).Erase(run.Id(), Access()).http_status == 409);
  }
  ObjectStore store(f.options);
  AgentSessionView view;
  Q4T_CHECK(Ok(ReadAgentSessionTurn(store, id, turn, Access(), &view)));
  Q4T_CHECK(view.session.GetString("status") == "idle" &&
            view.turn.GetString("status") == "completed" &&
            view.turn.GetInt("completed_at") >= view.turn.GetInt("started_at") &&
            view.turn.GetInt("started_at") > 0);
  Q4T_CHECK(view.items.array.size() == 2 &&
            view.items.array.back().GetArray("content")->array[0].GetString("text") == "42");
  Q4T_CHECK(view.session.Find("usage")->GetInt("total_tokens") == 14 &&
            !view.session.Find("usage")->Find("input_tokens_details")->Has("cache_write_tokens"));
  Q4T_CHECK(ReadAgentSessionTurn(store, id, "turn_other", Access(), &view).http_status == 404);
  Q4T_CHECK(ReadAgentSessionTurn(store, id, turn, Access("client:other"), &view).http_status == 404);
  return true;
}

Q4T_TEST(agent_session_tool_action_requires_a_completed_owned_call) {
  Fixture f;
  ObjectStore store(f.options);
  ResponsesExecution run(store);
  const auto request = Parse(R"({"agent":{"model":"cpu","tools":[
    {"type":"function","name":"lookup","description":"Read a project record",
     "parameters":{"type":"object","properties":{"revision":{"type":"integer"}}}}]},
    "environment":{"type":"none"},"input":"Look up revision 442."})");
  const auto now = std::time(nullptr);
  auto config = Config();
  Q4T_CHECK(PrepareAgentSessionExecution(store, request, config, Access(),
                                         now, run).code ==
            "structured_backend_unavailable");
  config.structured = true;
  auto prepared = PrepareAgentSessionExecution(store, request, config,
                                               Access(), now, run);
  Q4T_CHECK(Ok(prepared));
  const auto id = prepared.value.GetString("id");
  run.BindBackgroundControl(std::make_shared<RequestControl>(
      run.Id(), "", Clock::now() + 5s));
  std::string wire;
  Q4T_CHECK(Ok(run.Begin(&wire)) && Ok(run.StartRunning(&wire)));
  ResponseDelta delta;
  delta.kind = ResponseDelta::Kind::kTool;
  delta.call = {0, "call_lookup", "lookup", R"({"revision":442})"};
  Q4T_CHECK(run.Append(delta, "null", &wire));
  Q4T_CHECK(Ok(run.Checkpoint()));
  AgentSessionView view;
  Q4T_CHECK(Ok(ReadAgentSessionTurn(store, id, "", Access(), &view)));
  Q4T_CHECK(view.session.GetArray("required_actions")->array.empty());
  AgentEventCursor active_cursor;
  Json active_events;
  bool active_terminal = false;
  Q4T_CHECK(Ok(ReplayAgentSessionEvents(store, id, Access(), &active_cursor,
                                        &active_events, &active_terminal)));
  Q4T_CHECK(!active_terminal);
  for (const auto& event : active_events.array)
    Q4T_CHECK(event.GetString("type") != "agent.session.turn.item.added" ||
              event.Find("item")->GetString("type") != "function_call");
  Q4T_CHECK(Ok(run.Finish(false, 1, {9, 0, 8, 0}, std::time(nullptr), &wire)));
  Q4T_CHECK(Ok(ReadAgentSessionTurn(store, id, "", Access(), &view)));
  const auto& actions = view.session.GetArray("required_actions")->array;
  Q4T_CHECK(view.session.GetString("status") == "requires_action" &&
            view.turn.GetString("status") == "waiting" &&
            view.turn.Find("completed_at")->IsNull() && actions.size() == 1);
  Q4T_CHECK(actions[0].GetString("call_id") == "call_lookup" &&
            actions[0].Find("arguments")->GetInt("revision") == 442 &&
            actions[0].GetString("turn_id") == view.turn.GetString("id"));
  auto results = Parse(R"({"events":[{"type":"agent.session.input.tool_result",
    "turn_id":"unknown","call_id":"call_lookup","success":true,
    "output":"revision 442"}]})");
  ResponsesExecution next(store);
  bool replay = false;
  auto submit = [&](const Json& events, const char* key, ResponsesExecution& owner) {
    return PrepareAgentSessionEvents(store, id, events, key, config, Access(),
                                     owner, &replay);
  };
  Q4T_CHECK(submit(results, "lookup-wrong", next).http_status == 409);
  Set(&results.object[0].second.array[0], "turn_id", *view.turn.Find("id"));
  Q4T_CHECK(Ok(submit(results, "lookup-1", next)) && next.Prepared() && !replay);
  ResponsesExecution racing(store);
  Q4T_CHECK(Ok(submit(results, "lookup-1", racing)) && !replay);
  racing.BindBackgroundControl(std::make_shared<RequestControl>(
      racing.Id(), "", Clock::now() + 5s));
  next.BindBackgroundControl(std::make_shared<RequestControl>(
      next.Id(), "", Clock::now() + 5s));
  Q4T_CHECK(Ok(next.Begin(&wire)));
  auto lost_race = racing.Begin(&wire);
  Q4T_CHECK(lost_race.code == "object_revision_conflict");
  Q4T_CHECK(Ok(ReconcileAgentEventReceipt(store, id, results, "lookup-1",
                                          Access(), lost_race)));
  Q4T_CHECK(ResponsesState(store).Retrieve(racing.Id(), Access()).http_status == 404);
  ResponsesExecution retry(store);
  Q4T_CHECK(Ok(submit(results, "lookup-1", retry)) && replay && !retry.Prepared());
  auto conflict = results;
  Set(&conflict.object[0].second.array[0], "output", Parse(R"("different")"));
  Q4T_CHECK(submit(conflict, "lookup-1", retry).code == "idempotency_key_conflict");
  Q4T_CHECK(Ok(next.StartRunning(&wire)));
  delta = {};
  delta.kind = ResponseDelta::Kind::kContent;
  delta.text = "442";
  Q4T_CHECK(next.Append(delta, "null", &wire));
  Q4T_CHECK(Ok(next.Finish(false, 0, {15, 0, 1, 0}, std::time(nullptr), &wire)));
  Q4T_CHECK(Ok(ReadAgentSessionTurn(store, id, "", Access(), &view)));
  Q4T_CHECK(view.turns.array.size() == 1 &&
            view.turn.GetString("status") == "completed" &&
            view.items.array.size() == 4 &&
            view.items.array[2].GetString("type") == "function_call_output" &&
            view.items.array[2].GetString("output") == "revision 442" &&
            view.session.GetArray("required_actions")->array.empty() &&
            view.session.Find("usage")->GetInt("total_tokens") == 33);
  AgentEventCursor cursor;
  Json events;
  bool settled = false;
  std::set<std::string> identities;
  std::vector<std::string> types;
  std::string streamed_text;
  for (int batch = 0; batch < 8 && !settled; ++batch) {
    Q4T_CHECK(Ok(ReplayAgentSessionEvents(store, id, Access(), &cursor,
                                          &events, &settled)));
    Q4T_CHECK(!events.array.empty());
    for (const auto& event : events.array) {
      ChatPlanError invalid;
      Q4T_CHECK(ValidateAgentSession(event, AgentSessionShape::kEvent, &invalid));
      const auto event_id = event.GetString("event_id");
      Q4T_CHECK(event_id.starts_with("evt_") && event_id.find(run.Id()) == event_id.npos &&
                identities.insert(event_id).second);
      types.push_back(event.GetString("type"));
      if (types.back() == "agent.session.turn.item.added" &&
          event.Find("item")->GetString("type") == "function_call")
        Q4T_CHECK(event.Find("item")->Find("arguments")->GetInt("revision") == 442 &&
                  event.Find("item")->GetString("status") == "completed");
      if (types.back() == "agent.session.turn.output_text.delta")
        streamed_text += event.GetString("delta");
    }
  }
  Q4T_CHECK(settled && streamed_text == "442" &&
            types.front() == "agent.session.created" &&
            types.back() == "agent.session.idle");
  for (const auto* once : {"agent.session.turn.created", "agent.session.requires_action",
                           "agent.session.turn.completed"})
    Q4T_CHECK(std::count(types.begin(), types.end(), once) == 1);
  Q4T_CHECK(Ok(ReplayAgentSessionEvents(store, id, Access(), &cursor, &events, &settled)) &&
            events.array.empty() && !settled);
  AgentEventCursor peer;
  Q4T_CHECK(ReplayAgentSessionEvents(store, id, Access("client:other"), &peer,
                                     &events, &settled).http_status == 404);
  AgentEventCursor subscription;
  Q4T_CHECK(Ok(OpenAgentEventSubscription(store, id, Access(), &subscription)));
  Q4T_CHECK(Ok(ReplayAgentSessionEvents(store, id, Access(), &subscription, &events, &settled)) &&
            events.array.empty());
  const auto first_turn = view.turn.GetString("id");
  const auto follow = Parse(R"({"events":[{"type":"agent.session.input.message",
    "input":[{"role":"user","content":[{"type":"input_text","text":"again"}]}]}]})");
  ResponsesExecution later(store);
  Q4T_CHECK(Ok(submit(follow, "follow-1", later)) && later.Prepared() && !replay);
  later.BindBackgroundControl(std::make_shared<RequestControl>(
      later.Id(), "", Clock::now() + 5s));
  Q4T_CHECK(Ok(later.Begin(&wire)) &&
            Ok(ReadAgentSessionTurn(store, id, "", Access(), &view, true)));
  Q4T_CHECK(Ok(ReplayAgentSessionEvents(store, id, Access(), &subscription, &events, &settled)) &&
            !events.array.empty() && events.array.front().GetString("type") == "agent.session.turn.created" &&
            events.array.front().GetString("turn_id") != first_turn);
  Q4T_CHECK(view.turns.array.size() == 2 && view.turn.GetString("id") != first_turn &&
            view.items.array.size() == 5);
  Q4T_CHECK(Ok(ReadAgentSessionTurn(store, id, first_turn, Access(), &view)) &&
            view.items.array.size() == 4 && view.turn.GetString("status") == "completed");
  Q4T_CHECK(Ok(later.CloseUnfinished()));
  return true;
}

Q4T_TEST(
    agent_session_cancel_preserves_saved_results_and_never_retargets_a_retry) {
  Fixture f;
  ObjectStore store(f.options);
  auto config = Config();
  config.structured = true;
  const auto start = Parse(R"({"agent":{"model":"cpu","tools":[
    {"type":"function","name":"lookup","description":"Read a record","parameters":{"type":"object","properties":{}}}]},
    "environment":{"type":"none"},"input":"look up both project releases"})");
  ResponsesExecution run(store);
  auto session = PrepareAgentSessionExecution(store, start, config, Access(),
                                              std::time(nullptr), run);
  Q4T_CHECK(Ok(session));
  const auto id = session.value.GetString("id");
  run.BindBackgroundControl(
      std::make_shared<RequestControl>(run.Id(), "", Clock::now() + 5s));
  std::string wire;
  Q4T_CHECK(Ok(run.Begin(&wire)) && Ok(run.StartRunning(&wire)));
  for (size_t i = 0; i < 2; ++i) {
    ResponseDelta delta;
    delta.kind = ResponseDelta::Kind::kTool;
    delta.call = {i, "call_" + std::to_string(i), "lookup", "{}"};
    Q4T_CHECK(run.Append(delta, "null", &wire));
  }
  Q4T_CHECK(Ok(run.Finish(false, 2, {9, 0, 8, 0}, std::time(nullptr), &wire)));
  AgentSessionView view;
  Q4T_CHECK(Ok(ReadAgentSessionTurn(store, id, "", Access(), &view)) &&
            view.session.GetArray("required_actions")->array.size() == 2);
  const auto turn = view.turn.GetString("id");
  auto result = Parse(R"({"events":[{"type":"agent.session.input.tool_result",
    "turn_id":"","call_id":"call_0","success":true,"output":"release 442"}]})");
  Set(&result.object[0].second.array.front(), "turn_id", *view.turn.Find("id"));
  ResponsesExecution partial(store);
  bool replay = false;
  Q4T_CHECK(Ok(PrepareAgentSessionEvents(store, id, result, "saved", config,
                                         Access(), partial, &replay)) &&
            !partial.Prepared());
  AgentEventCursor live;
  Q4T_CHECK(Ok(OpenAgentEventSubscription(store, id, Access(), &live)) &&
            live.waiting);
  auto cancel = Parse(R"({"events":[{"type":"agent.session.input.cancel"}]})");
  Q4T_CHECK(CancelAgentSessionTurn(store, nullptr, id, cancel, "stop",
                                   Access("client:other"))
                .http_status == 404);
  Q4T_CHECK(
      CancelAgentSessionTurn(store, nullptr, id, cancel, "saved", Access())
          .http_status == 409);
  Q4T_CHECK(
      Ok(CancelAgentSessionTurn(store, nullptr, id, cancel, "stop", Access())));
  Q4T_CHECK(Ok(ReadAgentSessionTurn(store, id, turn, Access(), &view)) &&
            view.turn.GetString("status") == "cancelled" &&
            view.session.GetString("status") == "idle" &&
            view.session.GetArray("required_actions")->array.empty() &&
            view.items.array.back().GetString("output") == "release 442");
  Json events;
  bool settled = false;
  Q4T_CHECK(Ok(ReplayAgentSessionEvents(store, id, Access(), &live, &events,
                                        &settled)) &&
            settled && events.array.size() == 2 &&
            events.array.front().GetString("type") ==
                "agent.session.turn.cancelled" &&
            events.array.back().GetString("type") == "agent.session.idle");
  const auto once = events.array.front().GetString("event_id");
  Q4T_CHECK(Ok(ReplayAgentSessionEvents(store, id, Access(), &live, &events,
                                        &settled)) &&
            events.array.empty() && !settled);
  AgentEventCursor history;
  Q4T_CHECK(Ok(ReplayAgentSessionEvents(store, id, Access(), &history, &events,
                                        &settled)) &&
            settled);
  Q4T_CHECK(events.array.at(events.array.size() - 2).GetString("event_id") ==
            once);
  for (const auto& ev : events.array)
    Q4T_CHECK(ev.GetString("type") != "agent.session.turn.item.added" ||
              ev.Find("item")->GetString("type") != "function_call");
  auto follow =
      Parse(R"({"events":[{"type":"agent.session.input.message","input":[
    {"role":"user","content":[{"type":"input_text","text":"what was the saved result?"}]}]}]})");
  ResponsesExecution next(store);
  Q4T_CHECK(Ok(PrepareAgentSessionEvents(store, id, follow, "follow", config,
                                         Access(), next, &replay)));
  next.BindBackgroundControl(
      std::make_shared<RequestControl>(next.Id(), "", Clock::now() + 5s));
  Q4T_CHECK(Ok(next.Begin(&wire)));
  auto row = ResponsesState(store).ReadRecord(next.Id(), Access());
  Q4T_CHECK(Ok(row));
  const auto* inherited = row.value.GetArray("history");
  Q4T_CHECK(inherited &&
            std::ranges::count_if(
                inherited->array,
                [](const auto& item) {
                  return item.GetString("type") == "function_call_output" &&
                         item.GetString("output") == "release 442";
                }) == 1 &&
            std::ranges::none_of(inherited->array, [](const auto& item) {
              return item.GetString("call_id") == "call_1";
            }));
  Q4T_CHECK(
      Ok(CancelAgentSessionTurn(store, nullptr, id, cancel, "stop", Access())));
  bool cancelled = true;
  Q4T_CHECK(Ok(next.CheckParentCancellation(&cancelled)) && !cancelled);
  // Lose the signalling executor after writing the intent. Restored queued work
  // observes the same durable intent before starting the model.
  Q4T_CHECK(
      CancelAgentSessionTurn(store, nullptr, id, cancel, "stop-next", Access())
          .http_status == 503);
  ResponsesExecution restored(store);
  Q4T_CHECK(Ok(restored.RestoreQueued(next.Id(), config, Access())));
  Q4T_CHECK(Ok(restored.CheckParentCancellation(&cancelled)) && cancelled);
  Q4T_CHECK(Ok(next.Cancel()) &&
            Ok(CancelAgentSessionTurn(store, nullptr, id, cancel, "stop-next",
                                      Access())));
  int terminals = 0;
  for (int i = 0; i < 4; ++i) {
    Q4T_CHECK(Ok(ReplayAgentSessionEvents(store, id, Access(), &live, &events, &settled)));
    for (const auto& event : events.array)
      terminals += event.GetString("type") == "agent.session.turn.cancelled";
  }
  Q4T_CHECK(terminals == 1);
  return true;
}

Q4T_TEST(agent_session_turn_exhausted_object_quota_leaves_no_orphan) {
  Fixture f;
  f.options.max_client_objects = 1;
  ObjectStore store(f.options);
  ResponsesExecution run(store);
  const auto request = Parse(R"({"agent":{"model":"cpu"},
    "environment":{"type":"none"},"input":"project records"})");
  auto prepared = PrepareAgentSessionExecution(store, request, Config(), Access(),
                                               std::time(nullptr), run);
  Q4T_CHECK(Ok(prepared));
  run.BindBackgroundControl(std::make_shared<RequestControl>(run.Id(), "", Clock::now() + 5s));
  std::string wire;
  auto rejected = run.Begin(&wire);
  Q4T_CHECK(!rejected.ok() && !rejected.commit_unknown && rejected.http_status == 429);
  AgentSessionView view;
  Q4T_CHECK(ReadAgentSessionTurn(store, prepared.value.GetString("id"), "", Access(), &view).http_status == 404);
  Q4T_CHECK(ResponsesState(store).Retrieve(run.Id(), Access()).http_status == 404);
  return true;
}

Q4T_TEST(agent_session_settings_use_cas_filtered_pages_and_immutable_step_settings) {
  Fixture f;
  ObjectStore store(f.options);
  auto config = Config();
  config.structured = true;
  auto start = Parse(R"({"agent":{"model":"cpu"},"environment":{"type":"none"},
    "metadata":{"team":"unfiltered"},"input":"hello"})");
  for (int index = 0; index < 10; ++index) {
    ResponsesExecution run(store);
    auto created = PrepareAgentSessionExecution(store, start, config, Access(),
                                               std::time(nullptr), run);
    Q4T_CHECK(Ok(created));
    run.BindBackgroundControl(std::make_shared<RequestControl>(
        run.Id(), "", Clock::now() + 5s));
    std::string wire;
    Q4T_CHECK(Ok(run.Begin(&wire)) && Ok(run.StartRunning(&wire)));
    Q4T_CHECK(Ok(run.Finish(false, 0, {4, 0, 0, 0}, std::time(nullptr), &wire)));
    if (index != 0 && index != 9) continue;
    const auto id = created.value.GetString("id");
    auto update = Parse(R"({"metadata":{"team":"records"},"review":"auto_review",
      "spend_control":{"limit":25},"agent":{"reasoning":{"effort":"low"}}})");
    Q4T_CHECK(UpdateAgentSession(store, id, update, config, Access("client:other")).http_status == 404);
    auto updated = UpdateAgentSession(store, id, update, config, Access());
    Q4T_CHECK(Ok(updated) && updated.value.GetString("status") == "idle" &&
              updated.value.Find("spend_control")->GetInt("limit") == 25 &&
              updated.value.Find("agent")->Find("reasoning")->GetString("effort") == "low");
    AgentEventCursor cursor;
    Json events;
    bool settled = false;
    Q4T_CHECK(Ok(ReplayAgentSessionEvents(store, id, Access(), &cursor, &events, &settled)));
    const auto* snapshot = events.array.front().Find("session");
    Q4T_CHECK(snapshot && snapshot->GetString("review") == "none" &&
              snapshot->Find("metadata")->GetString("team") == "unfiltered");
  }
  auto query = Parse(R"({"metadata[team]":"records","limit":1,"order":"asc"})");
  auto page = ListAgentSessions(store, query, Access());
  Q4T_CHECK(Ok(page) && page.value.GetBool("has_more") &&
            page.value.GetArray("data")->array.size() == 1);
  const auto first = page.value.GetString("last_id");
  Set(&query, "after", *page.value.Find("last_id"));
  page = ListAgentSessions(store, query, Access());
  Q4T_CHECK(Ok(page) && !page.value.GetBool("has_more") &&
            page.value.GetArray("data")->array.size() == 1 &&
            page.value.GetString("last_id") != first);
  auto peer = ListAgentSessions(store, Parse("{}"), Access("client:other"));
  Q4T_CHECK(Ok(peer) && peer.value.GetArray("data")->array.empty());
  auto reset = UpdateAgentSession(store, first,
      Parse(R"({"metadata":null,"spend_control":{"limit":null},"agent":{"tools":null}})"),
      config, Access());
  Q4T_CHECK(Ok(reset) && reset.value.Find("metadata")->object.empty() &&
            !reset.value.Has("spend_control"));
  auto invalid = UpdateAgentSession(store, first, Parse(R"({"agent":{"model":"foreign"}})"),
                                    config, Access());
  Q4T_CHECK(invalid.http_status == 404);
  return true;
}

Q4T_TEST(agent_session_vaults_require_owned_identity_and_spend_limit_null_resets) {
  Fixture f;
  ObjectStore store(f.options);
  auto request = Parse(R"({"agent":{"model":"cpu"},"environment":{"type":"none"},
    "input":"records","vault_ids":["vault_1"],"spend_control":{"limit":null}})");
  ObjectRequest vault;
  vault.kind = ObjectKind::kVault;
  vault.operation = ObjectOperation::kPut;
  vault.scope = Access().scope;
  vault.id = "vault_1";
  vault.data = Parse(R"({"id":"vault_1","object":"vault"})");
  Q4T_CHECK(Ok(store.Execute(vault, Access().deadline)));
  AgentSessionStart start;
  Q4T_CHECK(Ok(PrepareDirectAgentSession(store, request, Config(), Access(), "s1", 1, &start)));
  Q4T_CHECK(!start.session.Has("spend_control") &&
            start.session.GetArray("vault_ids")->array.at(0).str == "vault_1");
  Q4T_CHECK(PrepareDirectAgentSession(store, request, Config(), Access("client:other"), "s1", 1, &start).http_status == 404);
  return true;
}

Q4T_TEST(agent_registration_owned_crud_defaults_and_stored_pagination) {
  Fixture f;
  ObjectStore store(f.options);
  const auto api = [&](std::string method, std::string path, std::string body = "",
                       StateAccess access = Access()) {
    return HandleAgentRegistration(store, method, path, body, Config(), access);
  };
  auto first = api("POST", "/v1/agents", R"({"model":"cpu","metadata":{"a":"1"},
    "multi_agent":{"enabled":true},"compaction":{},"tools":[{"type":"function",
    "name":"lookup","description":"Query","parameters":{}},{"type":"web_search"}]})");
  Q4T_CHECK(Ok(first));
  const auto id = first.value.GetString("id");
  const auto path = "/v1/agents/" + id;
  Q4T_CHECK(id.starts_with("agent_") && first.value.GetString("object") == "agent");
  Q4T_CHECK(first.value.Find("compaction")->GetBool("async"));
  Q4T_CHECK(first.value.Find("multi_agent")->GetInt("max_concurrent_subagents") == 6);
  Q4T_CHECK(!first.value.GetArray("tools")->array[0].GetBool("defer_loading"));
  Q4T_CHECK(first.value.GetArray("tools")->array[1].GetString("mode") == "live");
  Q4T_CHECK(api("GET", path, "", Access("client:foreign")).http_status == 404);
  auto changed = api("POST", path, R"({"name":"reader","metadata":null})");
  Q4T_CHECK(Ok(changed) && changed.value.GetString("name") == "reader" &&
            changed.value.Find("metadata")->object.empty());
  Q4T_CHECK(changed.value.GetArray("tools")->array.size() == 2);
  Q4T_CHECK(api("POST", path, R"({"multi_agent":{"enabled":true,"max_concurrent_subagents":0}})").http_status == 400);
  Q4T_CHECK(api("POST", path, R"({"model":"missing"})").http_status == 404);
  Q4T_CHECK(api("POST", path, R"({"tools":[{"type":"mcp","server_label":"docs",
    "transport":{"type":"http","server_url":"https://example.org","headers":{"Authorization":"secret"}}}]})").http_status == 400);
  Q4T_CHECK(api("GET", "/v1/agents?include=x").http_status == 400);
  Q4T_CHECK(api("GET", "/v1/agents/agent_").http_status == 404);
  Q4T_CHECK(api("PUT", path).http_status == 405);
  Q4T_CHECK(api("DELETE", path, "", Access("client:foreign")).http_status == 404);
  Q4T_CHECK(api("POST", path, R"({"unknown":true})").http_status == 400);
  Q4T_CHECK(api("POST", path, R"({"tools":[{"type":"mcp","server_label":"docs",
    "transport":{"type":"http","server_url":"https://exa mple.org"}}]})").http_status == 400);
  Q4T_CHECK(api("POST", path, R"({"tools":[{"type":"mcp","server_label":"docs",
    "transport":{"type":"http","server_url":"https://example.org","headers":{"x-note":"a\u0000b"}}}]})").http_status == 400);
  auto second = api("POST", "/v1/agents", R"({"model":"cpu"})");
  Q4T_CHECK(Ok(second));
  auto page = api("GET", "/v1/agents?order=asc&limit=1");
  Q4T_CHECK(Ok(page) && page.value.GetBool("has_more"));
  const auto cursor = page.value.GetString("last_id");
  auto next = api("GET", "/v1/agents?order=asc&limit=1&after=" + cursor);
  Q4T_CHECK(Ok(next) && !next.value.GetBool("has_more") &&
            next.value.GetString("last_id") != cursor);
  auto removed = api("DELETE", path);
  Q4T_CHECK(Ok(removed) && removed.value.GetString("object") == "agent.deleted" &&
            removed.value.GetString("id") == id && removed.value.GetBool("deleted"));
  Q4T_CHECK(api("GET", path).http_status == 404);
  return true;
}

Q4T_TEST(prompt_realtime_actual_business_definitions_follow_native_wire_schema) {
  const auto path = std::filesystem::path(__FILE__).parent_path().parent_path() /
                    "tools/serve_realtime/prompt_definitions.json";
  std::ifstream file(path);
  Q4T_CHECK(file.good());
  std::ostringstream bytes;
  bytes << file.rdbuf();
  auto definitions = Parse(bytes.str());
  Q4T_CHECK(definitions.IsObject() && definitions.object.size() == 5);
  Fixture f;
  ObjectStore store(f.options);
  PromptRegistry registry(store);
  for (const auto& [name, definition] : definitions.object) {
    (void)name;
    Q4T_CHECK(Ok(registry.Create(definition, Config(), Access())));
    auto bad = definition;
    SetRequest(&bad, "reasoning", Parse(R"({"effort":"none"})"));
    // Retain the actual original business failure as a negative: the Realtime
    // wire enum differs from internal Responses' default no-reasoning mapping.
    const auto result = registry.Create(bad, Config(), Access());
    Q4T_CHECK(result.http_status == 400 && result.code == "invalid_prompt");
  }
  return true;
}

Q4T_TEST(prompt_versions_are_immutable_and_default_selection_is_cas) {
  Fixture f;
  ObjectStore store(f.options);
  PromptRegistry registry(store);
  const auto first = registry.Create(Definition(), Config(), Access());
  Q4T_CHECK(Ok(first));
  Record(first.value);
  const auto id = first.value.GetString("id");
  Q4T_CHECK(first.value.GetInt("revision") == 1);
  auto original = registry.Retrieve(id, "1", Access());
  Q4T_CHECK(Ok(original));
  Record(original.value);
  auto second =
      registry.Append(id, 1, Definition("new {{value}}"), Config(), Access());
  Q4T_CHECK(Ok(second) && second.value.GetString("default_version") == "2");
  Q4T_CHECK(
      registry.Append(id, 1, Definition(), Config(), Access()).http_status ==
      409);
  Q4T_CHECK(OutputJson(registry.Retrieve(id, "1", Access()).value) ==
            OutputJson(original.value));
  Q4T_CHECK(registry.Select(id, 1, "1", Access()).http_status == 409);
  auto selected = registry.Select(id, 2, "1", Access());
  Q4T_CHECK(Ok(selected) && selected.value.GetInt("revision") == 3);
  Q4T_CHECK(registry.Select(id, 3, "99", Access()).http_status == 404);
  Q4T_CHECK(registry.Select(id, 3, "1", Access()).value.GetInt("revision") ==
            3);
  auto expanded = registry.Expand(Request(id), Config(), Access());
  Q4T_CHECK(Ok(expanded) &&
            expanded.value.Find("prompt")->GetString("version") == "1");
  return true;
}

Q4T_TEST(prompt_concurrent_append_has_one_committed_version) {
  Fixture f;
  ObjectStore store(f.options);
  PromptRegistry registry(store);
  auto first = registry.Create(Definition(), Config(), Access());
  Q4T_CHECK(Ok(first));
  const auto id = first.value.GetString("id");
  ObjectReply left, right;
  std::jthread a([&] {
    left =
        registry.Append(id, 1, Definition("a {{value}}"), Config(), Access());
  });
  std::jthread b([&] {
    right =
        registry.Append(id, 1, Definition("b {{value}}"), Config(), Access());
  });
  a.join();
  b.join();
  Q4T_CHECK(left.ok() != right.ok());
  Q4T_CHECK((left.ok() ? right : left).http_status == 409);
  auto read = registry.Retrieve(id, "", Access());
  Q4T_CHECK(Ok(read));
  Q4T_CHECK(read.value.GetInt("revision") == 2 &&
            read.value.GetArray("versions")->array.size() == 2);
  return true;
}

Q4T_TEST(prompt_scope_isolates_lookup_mutation_and_pagination) {
  Fixture f;
  ObjectStore store(f.options);
  PromptRegistry registry(store);
  auto one = registry.Create(Definition(), Config(), Access());
  Q4T_CHECK(Ok(one));
  auto two = registry.Create(Definition(), Config(), Access());
  Q4T_CHECK(Ok(two));
  const auto id = one.value.GetString("id");
  auto other = Access("client:prompt-b");
  Q4T_CHECK(registry.Retrieve(id, "", other).http_status == 404);
  Q4T_CHECK(registry.Expand(Request(id), Config(), other).http_status == 404);
  Q4T_CHECK(registry.Append(id, 1, Definition(), Config(), other).http_status ==
            404);
  Q4T_CHECK(registry.Select(id, 1, "1", other).http_status == 404);
  Q4T_CHECK(registry.Erase(id, other).http_status == 404);
  ObjectRequest page;
  page.limit = 1;
  page.descending = false;
  auto start = registry.List(page, Access());
  Q4T_CHECK(Ok(start));
  Record(start.value);
  Q4T_CHECK(start.value.GetBool("has_more") &&
            start.value.GetArray("data")->array.size() == 1);
  page.after = start.value.GetString("last_id");
  auto end = registry.List(page, Access());
  Q4T_CHECK(Ok(end));
  Q4T_CHECK(!end.value.GetBool("has_more") &&
            end.value.GetString("first_id") != page.after);
  page.after.clear();
  auto empty = registry.List(page, other);
  Q4T_CHECK(Ok(empty) && empty.value.GetArray("data")->array.empty());
  return true;
}

Q4T_TEST(prompt_substitution_is_single_pass_and_preserves_typed_media) {
  Fixture f;
  ObjectStore store(f.options);
  PromptRegistry registry(store);
  auto definition = Parse(R"({"request":{"instructions":"Say {{label}}",
    "input":[{"role":"user","content":[{"type":"input_text","text":"Read {{text}} {{photo}} {{doc}}"}]}]},
    "variables":{"label":"string","text":"input_text","photo":"input_image","doc":"input_file"}})");
  auto created = registry.Create(definition, Config(), Access());
  Q4T_CHECK(Ok(created));
  auto request =
      Parse("{\"prompt\":{\"id\":\"" + created.value.GetString("id") + R"(",
    "variables":{"label":"{{text}} \\\n\"literal\"","text":{"type":"input_text","text":"{{label}}"},
    "photo":{"type":"input_image","image_url":"data:image/png;base64,AA==","detail":"low"},
    "doc":{"type":"input_file","file_id":"file_real"}}}})");
  auto expanded = registry.Expand(request, Config(), Access());
  Q4T_CHECK(Ok(expanded));
  Q4T_CHECK(expanded.value.GetString("instructions").find("{{text}}") !=
            std::string::npos);
  const auto& content =
      expanded.value.GetArray("input")->array[0].GetArray("content")->array;
  Q4T_CHECK(content.size() == 6 && content[1].GetString("text") == "{{label}}");
  Q4T_CHECK(content[3].GetString("image_url") == "data:image/png;base64,AA==");
  Q4T_CHECK(content[5].GetString("file_id") == "file_real");
  auto bad = request;
  auto ref = *bad.Find("prompt");
  auto vars = *ref.Find("variables");
  Set(&vars, "text", Parse("42"));
  Set(&ref, "variables", vars);
  Set(&bad, "prompt", ref);
  Q4T_CHECK(registry.Expand(bad, Config(), Access()).http_status == 400);
  return true;
}

Q4T_TEST(prompt_missing_extra_and_media_instruction_variables_are_rejected) {
  Fixture f;
  ObjectStore store(f.options);
  PromptRegistry registry(store);
  auto created = registry.Create(Definition(), Config(), Access());
  Q4T_CHECK(Ok(created));
  const auto id = created.value.GetString("id");
  for (const auto* values : {"null", "[]", "{}", R"({"name":"n","value":42})",
                             R"({"name":"n","value":"v","extra":"x"})"}) {
    auto request = Request(id);
    auto reference = *request.Find("prompt");
    Set(&reference, "variables", Parse(values));
    Set(&request, "prompt", reference);
    Q4T_CHECK(registry.Expand(request, Config(), Access()).http_status == 400);
  }
  auto media = Parse(
      R"({"request":{"instructions":"{{image}}"},"variables":{"image":"input_image"}})");
  Q4T_CHECK(registry.Create(media, Config(), Access()).http_status == 400);
  auto unused = Parse(
      R"({"request":{"instructions":"literal"},"variables":{"unused":"string"}})");
  Q4T_CHECK(registry.Create(unused, Config(), Access()).http_status == 400);
  return true;
}

Q4T_TEST(
    prompt_large_file_variable_preserves_source_without_inline_copy_limit) {
  Fixture f;
  ObjectStore store(f.options);
  PromptRegistry registry(store);
  const auto definition = Parse(R"({"request":{"input":[{"role":"user",
    "content":"Read {{file}}"}]},"variables":{"file":"input_file"}})");
  const auto created = registry.Create(definition, Config(), Access());
  Q4T_CHECK(Ok(created));
  std::string encoded(4 * ((50000000ULL + 2) / 3), 'A');
  encoded.back() = '=';
  auto request =
      Parse("{\"prompt\":{\"id\":\"" + created.value.GetString("id") +
            R"(","variables":{"file":{
        "type":"input_file","filename":"capacity.bin","file_data":")" +
            encoded + R"("}}}})");
  const auto result = registry.Expand(request, Config(), Access());
  Q4T_CHECK(Ok(result));
  const auto& content =
      result.value.GetArray("input")->array[0].GetArray("content")->array;
  Q4T_CHECK(content.size() == 2 &&
            content[1].GetString("file_data") == encoded);
  Q4T_CHECK(content[1].GetString("filename") == "capacity.bin");
  return true;
}

Q4T_TEST(prompt_text_substitution_preserves_exact_cache_boundary) {
  Fixture f;
  ObjectStore store(f.options);
  PromptRegistry registry(store);
  auto definition = Parse(R"({"request":{"input":[{"role":"user","content":[
    {"type":"input_text","text":"start {{x}} end","prompt_cache_breakpoint":{"mode":"explicit","ttl":"12h"}}]}]},
    "variables":{"x":"input_text"}})");
  auto created = registry.Create(definition, Config(), Access());
  Q4T_CHECK(Ok(created));
  auto request = Parse(
      "{\"prompt\":{\"id\":\"" + created.value.GetString("id") +
      R"(","variables":{"x":{"type":"input_text","text":"value","prompt_cache_breakpoint":{"mode":"explicit","ttl":"30m"}}}}})");
  auto expanded = registry.Expand(request, Config(), Access());
  Q4T_CHECK(Ok(expanded));
  const auto& parts =
      expanded.value.GetArray("input")->array[0].GetArray("content")->array;
  Q4T_CHECK(parts.size() == 3 && !parts[0].Has("prompt_cache_breakpoint"));
  Q4T_CHECK(parts[1].Find("prompt_cache_breakpoint")->GetString("ttl") ==
            "30m");
  Q4T_CHECK(parts[2].Find("prompt_cache_breakpoint")->GetString("ttl") ==
            "12h");
  return true;
}

Q4T_TEST(prompt_version_capacity_and_cancel_preserve_existing_state) {
  Fixture f;
  ObjectStore store(f.options);
  PromptRegistry registry(store);
  auto created = registry.Create(Definition(), Config(), Access());
  Q4T_CHECK(Ok(created));
  const auto id = created.value.GetString("id");
  for (int64_t version = 1; version < 64; ++version)
    Q4T_CHECK(
        Ok(registry.Append(id, version, Definition(), Config(), Access())));
  auto before = registry.Retrieve(id, "", Access());
  Q4T_CHECK(Ok(before));
  Q4T_CHECK(registry.Append(id, 64, Definition(), Config(), Access()).code ==
            "prompt_version_capacity");
  auto cancelled = Access();
  cancelled.cancelled = [] { return true; };
  Q4T_CHECK(!registry.Select(id, 64, "1", cancelled).ok());
  auto expired = Access();
  expired.deadline = Clock::now() - 1ms;
  Q4T_CHECK(!registry.Select(id, 64, "1", expired).ok());
  auto after = registry.Retrieve(id, "", Access());
  Q4T_CHECK(Ok(after));
  Q4T_CHECK(OutputJson(before.value) == OutputJson(after.value));
  return true;
}

Q4T_TEST(prompt_definition_cannot_set_authority_or_rewrite_message_roles) {
  Fixture f;
  ObjectStore store(f.options);
  PromptRegistry registry(store);
  for (const auto* field : {"store", "background", "conversation", "prompt",
                            "client_id", "request_timeout_ms"}) {
    auto definition = Definition();
    auto request = *definition.Find("request");
    Set(&request, field, Parse("null"));
    Set(&definition, "request", request);
    Q4T_CHECK(registry.Create(definition, Config(), Access()).http_status ==
              400);
  }
  auto role = Parse(
      R"({"request":{"input":[{"role":"{{role}}","content":"x"}]},"variables":{"role":"string"}})");
  Q4T_CHECK(registry.Create(role, Config(), Access()).http_status == 400);
  auto text_extra = Parse(
      R"({"request":{"input":[{"role":"user","content":[{"type":"input_text","text":"{{x}}","unknown":1}]}]},"variables":{"x":"string"}})");
  Q4T_CHECK(registry.Create(text_extra, Config(), Access()).http_status == 400);
  return true;
}

Q4T_TEST(prompt_effective_request_uses_overrides_and_actual_context) {
  Fixture f;
  ObjectStore store(f.options);
  PromptRegistry registry(store);
  auto created = registry.Create(Definition(), Config(), Access());
  Q4T_CHECK(Ok(created));
  auto request =
      Request(created.value.GetString("id"),
              R"(,"instructions":"override","temperature":0.5,"store":false)");
  ResponsesExecution run(store);
  Q4T_CHECK(Ok(run.Prepare(request, Config(), Access(), 42)));
  Q4T_CHECK(run.Envelope().options.GetString("instructions") == "override");
  Q4T_CHECK(run.Envelope().options.Find("temperature")->number == 0.5);
  Q4T_CHECK(!run.Envelope().store);
  const auto& chat = run.Generation().generation_request;
  std::string prompt, param, code;
  std::vector<VisionItem> media;
  Q4T_CHECK(PrepareChatPrompt(run.Generation().chat.Request(chat),
                              run.Generation().chat.constraints, &prompt,
                              &media, &param, &code));
  Q4T_CHECK(prompt.find("override") != prompt.npos &&
            prompt.find("42") != prompt.npos &&
            prompt.find("current") != prompt.npos);
  Q4T_CHECK(prompt.find("Answer reader") == prompt.npos &&
            prompt.find("{{") == prompt.npos);
  Q4T_CHECK(run.Envelope().options.Find("prompt")->GetString("version") == "1");
  const auto* reference = run.Output().Snapshot().Find("prompt");
  Q4T_CHECK(reference &&
            OutputJson(*reference) ==
                OutputJson(*run.Envelope().options.Find("prompt")));
  return true;
}

Q4T_TEST(prompt_expansion_cannot_bypass_client_output_or_input_bounds) {
  Fixture f;
  ObjectStore store(f.options);
  PromptRegistry registry(store);
  auto definition = Definition();
  auto req = *definition.Find("request");
  Set(&req, "max_output_tokens", Parse("128"));
  Set(&definition, "request", req);
  auto created = registry.Create(definition, Config(), Access());
  Q4T_CHECK(Ok(created));
  auto config = Config();
  ClientLimits limits;
  limits.max_output_tokens = 8;
  config.client_limits = &limits;
  ResponsesExecution run(store);
  auto result =
      run.Prepare(Request(created.value.GetString("id")), config, Access(), 42);
  Q4T_CHECK(result.http_status == 400 &&
            result.code == "client_output_limit_exceeded");
  auto override =
      Request(created.value.GetString("id"), R"(,"max_output_tokens":8)");
  ResponsesExecution allowed(store);
  Q4T_CHECK(Ok(allowed.Prepare(override, config, Access(), 42)));
  auto too_large = Definition(std::string(262144, 'x') + "{{value}}");
  Q4T_CHECK(registry.Create(too_large, Config(), Access()).http_status == 413);
  std::string repeated;
  for (int i = 0; i < 512; ++i) repeated += "{{value}}";
  auto amplified = registry.Create(Definition(repeated), Config(), Access());
  Q4T_CHECK(Ok(amplified));
  auto big = Request(amplified.value.GetString("id"));
  auto ref = *big.Find("prompt");
  auto vars = *ref.Find("variables");
  Json text;
  text.type = Json::Type::kString;
  text.str.assign(400000, 'x');
  Set(&vars, "value", text);
  Set(&ref, "variables", vars);
  Set(&big, "prompt", ref);
  Q4T_CHECK(registry.Expand(big, Config(), Access()).code ==
            "prompt_expansion_limit");
  return true;
}

Q4T_TEST(
    prompt_queue_restores_frozen_version_after_registry_change_and_delete) {
  Fixture f;
  ObjectStore store(f.options);
  PromptRegistry registry(store);
  auto created = registry.Create(Definition(), Config(), Access());
  Q4T_CHECK(Ok(created));
  const auto id = created.value.GetString("id");
  ResponsesExecution queued(store);
  Q4T_CHECK(
      Ok(queued.Prepare(Request(id, R"(,"background":true,"stream":true)"),
                        Config(), Access(), 42, true)));
  const auto original = OutputJson(queued.Generation().generation_request);
  std::string wire;
  Q4T_CHECK(Ok(queued.Begin(&wire)));
  Q4T_CHECK(Ok(registry.Append(id, 1, Definition("changed {{value}}"), Config(),
                               Access())));
  Q4T_CHECK(Ok(registry.Erase(id, Access())));
  ResponsesExecution restored(store);
  Q4T_CHECK(Ok(restored.RestoreQueued(queued.Id(), Config(), Access())));
  Q4T_CHECK(OutputJson(restored.Generation().generation_request) == original);
  Q4T_CHECK(restored.Envelope().options.Find("prompt")->GetString("version") ==
            "1");
  Q4T_CHECK(Ok(restored.StartRunning(&wire)));
  ResponseDelta text;
  text.kind = ResponseDelta::Kind::kContent;
  text.text = "42";
  Q4T_CHECK(restored.Append(text, "null", &wire));
  Q4T_CHECK(Ok(restored.Finish(false, 0, {12, 0, 2, 0}, 43, &wire)));
  const auto* reference = restored.Output().Snapshot().Find("prompt");
  Q4T_CHECK(reference && reference->GetString("id") == id &&
            reference->GetString("version") == "1");
  Record(restored.Output().Snapshot());
  auto record = ResponsesState(store).ReadRecord(queued.Id(), Access());
  Q4T_CHECK(Ok(record));
  Q4T_CHECK(record.value.Find("request")->Find("prompt")->GetString(
                "version") == "1");
  Q4T_CHECK(record.value.GetArray("history")->array.size() == 2);
  for (const auto& event : record.value.GetArray("events")->array) {
    if (const auto* response = event.Find("response")) {
      Q4T_CHECK(response->Find("prompt") &&
                OutputJson(*response->Find("prompt")) ==
                    OutputJson(*reference));
    }
    Record(event);
  }
  return true;
}

Q4T_TEST(prompt_registry_restart_preserves_versions_and_default) {
  Fixture f;
  std::string id, previous;
  {
    ObjectStore store(f.options);
    PromptRegistry registry(store);
    auto created = registry.Create(Definition(), Config(), Access());
    Q4T_CHECK(Ok(created));
    id = created.value.GetString("id");
    Q4T_CHECK(Ok(registry.Append(id, 1, Definition("v2 {{value}}"), Config(),
                                 Access())));
    previous = OutputJson(registry.Retrieve(id, "1", Access()).value);
  }
  ObjectStore store(f.options);
  PromptRegistry registry(store);
  auto read = registry.Retrieve(id, "", Access());
  Q4T_CHECK(Ok(read));
  Q4T_CHECK(read.value.GetString("default_version") == "2");
  Q4T_CHECK(OutputJson(registry.Retrieve(id, "1", Access()).value) == previous);
  auto expanded = registry.Expand(Request(id), Config(), Access());
  Q4T_CHECK(Ok(expanded));
  Q4T_CHECK(OutputJson(expanded.value).find("v2 ") != std::string::npos);
  return true;
}

Q4T_TEST(prompt_storage_failure_does_not_block_plain_stateless_generation) {
  Fixture f;
  f.options.directory = "/proc/q4t-prompts-no-storage";
  ObjectStore store(f.options);
  PromptRegistry registry(store);
  auto failed = registry.Expand(Request("prompt_none"), Config(), Access());
  Q4T_CHECK(!failed.ok());
  ResponsesExecution plain(store);
  Q4T_CHECK(Ok(plain.Prepare(
      Parse(
          R"({"input":"ordinary","store":false,"reasoning":{"effort":"none"}})"),
      Config(), Access(), 42)));
  return true;
}

Q4T_TEST(prompt_api_enforces_routes_revision_body_and_query) {
  Fixture f;
  ObjectStore store(f.options);
  Json body = Parse("{}");
  Set(&body, "definition", Definition());
  auto created = Api(store, "POST", "/v1/prompts", body);
  Q4T_CHECK(Ok(created));
  const auto path = "/v1/prompts/" + created.value.GetString("id");
  Q4T_CHECK(Api(store, "POST", path + "/versions", body).http_status == 400);
  Set(&body, "expected_revision", Parse("1"));
  auto append = Api(store, "POST", path + "/versions", body);
  Q4T_CHECK(Ok(append));
  Q4T_CHECK(Api(store, "POST", path + "/versions", body).http_status == 409);
  auto version = Api(store, "GET", path + "/versions/2");
  Q4T_CHECK(Ok(version));
  Record(version.value);
  Q4T_CHECK(Api(store, "POST", path,
                Parse(R"({"default_version":"1","expected_revision":2})"))
                .ok());
  for (const auto* suffix :
       {"?owner=other", "/versions/02", "/versions/2/more", "#fragment"})
    Q4T_CHECK(!Api(store, "GET", path + suffix).ok());
  Q4T_CHECK(Api(store, "GET", path, Parse("{}")).http_status == 400);
  Q4T_CHECK(Api(store, "PUT", path, body).http_status == 405);
  Q4T_CHECK(Api(store, "POST", path + "/versions/2", body).http_status == 405);
  Q4T_CHECK(Api(store, "GET", "/v1/prompts?limit=0").http_status == 400);
  Q4T_CHECK(Api(store, "GET", "/v1/prompts?limit=1&limit=2").http_status ==
            400);
  Q4T_CHECK(Api(store, "GET", "/v1/prompts?order=unknown").http_status == 400);
  auto list = Api(store, "GET", "/v1/prompts?limit=1&order=asc");
  Q4T_CHECK(Ok(list));
  Record(list.value);
  auto erased = Api(store, "DELETE", path);
  Q4T_CHECK(Ok(erased));
  Record(erased.value);
  Q4T_CHECK(Api(store, "GET", path).http_status == 404);
  return true;
}

Q4T_TEST(prompt_management_route_partitions_reads_and_writes) {
  for (const auto* path : {"/v1/prompts", "/v1/prompts/prompt_a",
                           "/v1/prompts/prompt_a/versions/1"}) {
    Q4T_CHECK(PromptMethodAllowed("GET", path));
    Q4T_CHECK(ClassifyHttpConnection("GET", path) ==
              HttpConnectionClass::kObservation);
    Q4T_CHECK(!IsCancellationRequest("GET", path));
  }
  for (const auto* path : {"/v1/prompts", "/v1/prompts/prompt_a",
                           "/v1/prompts/prompt_a/versions"}) {
    Q4T_CHECK(PromptMethodAllowed("POST", path));
    Q4T_CHECK(ClassifyHttpConnection("POST", path) ==
              HttpConnectionClass::kBusiness);
  }
  Q4T_CHECK(!PromptMethodAllowed("DELETE", "/v1/prompts/prompt_a/versions/1"));
  Q4T_CHECK(!PromptMethodAllowed("GET", "/v1/prompts/../versions/1"));
  Q4T_CHECK(
      !PromptMethodAllowed("GET", "/v1/prompts/prompt_a/versions/1/extra"));
  return true;
}

Q4T_TEST(prompt_snapshot_pins_version_and_survives_registry_deletion) {
  Fixture f;
  ObjectStore store(f.options);
  PromptRegistry registry(store);
  auto created = registry.Create(Definition(), Config(), Access());
  Q4T_CHECK(Ok(created));
  const auto id = created.value.GetString("id");
  const auto reference = *Request(id).Find("prompt");
  PromptSnapshot original;
  Q4T_CHECK(Ok(registry.Resolve(reference, Access(), &original)));
  const auto bytes = OutputJson(original.request);
  Q4T_CHECK(original.api == PromptApi::kResponses &&
            original.scope == Access().scope &&
            original.reference.GetString("version") == "1");
  Q4T_CHECK(Ok(
      registry.Append(id, 1, Definition("new {{value}}"), Config(), Access())));
  PromptSnapshot latest;
  Q4T_CHECK(Ok(registry.Resolve(reference, Access(), &latest)));
  Q4T_CHECK(latest.reference.GetString("version") == "2" &&
            OutputJson(latest.request) != bytes);
  Q4T_CHECK(Ok(registry.Select(id, 2, "1", Access())));
  Q4T_CHECK(Ok(registry.Erase(id, Access())));
  Q4T_CHECK(OutputJson(original.request) == bytes &&
            original.reference.GetString("version") == "1");
  Q4T_CHECK(registry.Resolve(reference, Access(), &original).http_status ==
            404);
  Q4T_CHECK(OutputJson(original.request) == bytes);
  return true;
}

Q4T_TEST(prompt_snapshot_scope_cancellation_and_deadline_are_atomic) {
  Fixture f;
  ObjectStore store(f.options);
  PromptRegistry registry(store);
  auto created = registry.Create(Definition(), Config(), Access());
  Q4T_CHECK(Ok(created));
  const auto reference = *Request(created.value.GetString("id")).Find("prompt");
  PromptSnapshot snapshot;
  Q4T_CHECK(Ok(registry.Resolve(reference, Access(), &snapshot)));
  const auto before = OutputJson(snapshot.request);
  Q4T_CHECK(registry.Resolve(reference, Access("client:other"), &snapshot)
                .http_status == 404);
  auto cancelled = Access();
  cancelled.cancelled = [] { return true; };
  Q4T_CHECK(!registry.Resolve(reference, cancelled, &snapshot).ok());
  auto expired = Access();
  expired.deadline = Clock::now() - 1ms;
  Q4T_CHECK(!registry.Resolve(reference, expired, &snapshot).ok());
  Q4T_CHECK(snapshot.scope == Access().scope &&
            OutputJson(snapshot.request) == before &&
            snapshot.reference.GetString("version") == "1");
  Q4T_CHECK(registry.Resolve(reference, Access(), nullptr).http_status == 500);
  return true;
}

Q4T_TEST(prompt_realtime_definition_keeps_session_fields_and_hidden_prefix) {
  Fixture f;
  ObjectStore store(f.options);
  PromptRegistry registry(store);
  const auto definition = Parse(R"({"api":"realtime","variables":{
    "name":"string","task":"string"},"request":{
    "instructions":"Address {{name}}", "input":[{"role":"user",
    "content":"{{task}}"}], "output_modalities":["text"],
    "max_output_tokens":45,"parallel_tool_calls":false,
    "reasoning":{"effort":"low"},"truncation":"disabled",
    "tools":[{"type":"function","name":"lookup","parameters":{
      "type":"object","properties":{"key":{"type":"string"}}}}],
    "tool_choice":{"type":"function","name":"lookup"},
    "audio":{"input":{"noise_reduction":null,"transcription":null}},
    "tracing":null}})");
  auto created = registry.Create(definition, Config(), Access());
  Q4T_CHECK(Ok(created));
  auto reference = Parse("{\"id\":\"" + created.value.GetString("id") +
                         R"(","variables":{"name":"reader",
                             "task":"{{name}} stays literal"}})");
  PromptSnapshot snapshot;
  Q4T_CHECK(Ok(registry.Resolve(reference, Access(), &snapshot)));
  Q4T_CHECK(snapshot.api == PromptApi::kRealtime &&
            snapshot.request.GetString("type") == "realtime" &&
            snapshot.request.GetString("instructions") == "Address reader");
  Q4T_CHECK(snapshot.request.GetArray("input")
                ->array[0]
                .GetArray("content")
                ->array[0]
                .GetString("text") == "{{name}} stays literal");
  for (const auto* key :
       {"output_modalities", "max_output_tokens", "parallel_tool_calls",
        "reasoning", "truncation", "tools", "tool_choice", "audio", "tracing"})
    Q4T_CHECK(OutputJson(*snapshot.request.Find(key)) ==
              OutputJson(*definition.Find("request")->Find(key)));
  return true;
}

Q4T_TEST(prompt_realtime_management_persists_api_across_restart) {
  Fixture f;
  std::string id;
  {
    ObjectStore store(f.options);
    auto created = Api(store, "POST", "/v1/prompts", Parse(R"({
      "definition":{"api":"realtime","request":{
        "instructions":"Use the actual tool result","max_output_tokens":"inf"
      },"variables":{}}})"));
    Q4T_CHECK(Ok(created));
    id = created.value.GetString("id");
  }
  ObjectStore restored(f.options);
  auto version = Api(restored, "GET", "/v1/prompts/" + id + "/versions/1");
  Q4T_CHECK(Ok(version) &&
            version.value.Find("definition")->GetString("api") == "realtime");
  PromptRegistry registry(restored);
  PromptSnapshot snapshot;
  Q4T_CHECK(Ok(
      registry.Resolve(Parse("{\"id\":\"" + id + "\"}"), Access(), &snapshot)));
  Q4T_CHECK(snapshot.api == PromptApi::kRealtime &&
            snapshot.request.GetString("max_output_tokens") == "inf");
  return true;
}

Q4T_TEST(prompt_api_types_do_not_silently_reinterpret_templates) {
  Fixture f;
  ObjectStore store(f.options);
  PromptRegistry registry(store);
  auto definition = Parse(R"({"api":"realtime","request":{
    "instructions":"template"},"variables":{}})");
  auto created = registry.Create(definition, Config(), Access());
  Q4T_CHECK(Ok(created));
  const auto id = created.value.GetString("id");
  auto request = Parse("{\"prompt\":{\"id\":\"" + id + "\"}}");
  Q4T_CHECK(registry.Expand(request, Config(), Access()).http_status == 400);
  auto responses = Definition();
  Set(&responses, "api", Parse("\"responses\""));
  auto response_prompt = registry.Create(responses, Config(), Access());
  Q4T_CHECK(Ok(response_prompt));
  Q4T_CHECK(Ok(registry.Expand(Request(response_prompt.value.GetString("id")),
                               Config(), Access())));
  for (const auto* api : {"null", "false", "7", "\"other\""}) {
    Set(&definition, "api", Parse(api));
    Q4T_CHECK(registry.Create(definition, Config(), Access()).http_status ==
              400);
  }
  return true;
}

Q4T_TEST(prompt_realtime_rejects_authority_recursive_prompt_and_bad_shape) {
  Fixture f;
  ObjectStore store(f.options);
  PromptRegistry registry(store);
  const auto base = Parse(R"({"api":"realtime","request":{},"variables":{}})");
  for (const auto* field : {"prompt", "store", "background", "conversation",
                            "client_id", "request_timeout_ms", "id"}) {
    auto definition = base;
    SetRequest(&definition, field, Parse("null"));
    Q4T_CHECK(registry.Create(definition, Config(), Access()).http_status ==
              400);
  }
  for (const auto* request : {R"({"type":"transcription"})",
                              R"({"audio":{"input":{"unknown":true}}})",
                              R"({"output_modalities":["video"]})"}) {
    auto definition = base;
    Set(&definition, "request", Parse(request));
    Q4T_CHECK(registry.Create(definition, Config(), Access()).http_status ==
              400);
  }
  return true;
}

Q4T_TEST(prompt_realtime_credentials_cannot_be_persisted_in_tools_or_prefix) {
  Fixture f;
  ObjectStore store(f.options);
  PromptRegistry registry(store);
  const auto base = Parse(R"({"api":"realtime","request":{"tools":[{
    "type":"mcp","server_label":"docs","server_url":"https://example.test/mcp"
  }]},"variables":{}})");
  Q4T_CHECK(Ok(registry.Create(base, Config(), Access())));
  for (const auto* field : {"authorization", "headers"}) {
    auto definition = base;
    auto tools = *definition.Find("request")->Find("tools");
    Set(&tools.array[0], field,
        Parse(std::string(field) == "headers"
                  ? R"({"Authorization":"fixture-credential"})"
                  : R"("fixture-credential")"));
    SetRequest(&definition, "tools", tools);
    Q4T_CHECK(registry.Create(definition, Config(), Access()).http_status ==
              400);
  }
  auto definition = base;
  SetRequest(&definition, "input", Parse(R"([{
    "type":"tool_search_output","tools":[{"type":"mcp",
    "server_label":"other","server_url":"https://example.test/mcp",
    "authorization":"fixture-credential"}]}])"));
  Q4T_CHECK(registry.Create(definition, Config(), Access()).http_status == 400);
  return true;
}

Q4T_TEST(prompt_realtime_token_limit_does_not_accept_fraction_or_overflow) {
  Fixture f;
  ObjectStore store(f.options);
  PromptRegistry registry(store);
  for (const auto* value : {"0", "1.5", "4097", "null", "true"}) {
    auto definition =
        Parse(R"({"api":"realtime","variables":{},"request":{}})");
    SetRequest(&definition, "max_output_tokens", Parse(value));
    Q4T_CHECK(registry.Create(definition, Config(), Access()).http_status ==
              400);
  }
  return true;
}
