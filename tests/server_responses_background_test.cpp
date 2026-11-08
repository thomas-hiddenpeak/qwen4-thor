#include "q4t/server/responses_execution.h"

#include <unistd.h>

#include <cstdlib>
#include <filesystem>
#include <thread>

#include "q4t/server/conversation_state.h"
#include "q4t/server/output_schema.h"
#include "q4t/server/responses_api.h"
#include "q4t/test.h"

namespace {
using namespace q4t::server;
using q4t::io::Json;
using Clock = std::chrono::steady_clock;
using namespace std::chrono_literals;
Json Parse(const std::string& text) {
  Json value;
  if (!q4t::io::ParseJson(text, &value, true))
    throw std::runtime_error("background fixture JSON");
  return value;
}
bool Ok(const ObjectReply& reply) {
  if (!reply.ok())
    std::fprintf(stderr, "%s: %s\n", reply.code.c_str(), reply.message.c_str());
  return reply.ok();
}
struct Fixture {
  ObjectStoreOptions options;
  Fixture() {
    const auto* output = std::getenv("Q4T_TEST_OUTPUT_DIR");
    const auto root = std::filesystem::absolute(
        output ? output : ".q4t-work/responses-background-cpu");
    std::filesystem::create_directories(root);
    auto directory = (root / "background-XXXXXX").string();
    if (!mkdtemp(directory.data())) throw std::runtime_error("background path");
    options.directory = directory + "/objects";
    options.restart_backoff_ms = 0;
  }
};
StateAccess Access(std::string scope = "client:cpu") {
  return {std::move(scope), Clock::now() + 5s, {}};
}
ChatPlanConfig Config() {
  ChatPlanConfig config;
  config.model = "cpu";
  return config;
}
ObjectReply Prepare(ResponsesExecution& run, std::string extra = "") {
  return run.Prepare(Parse(R"({"input":"question","background":true,
        "reasoning":{"effort":"none"})" +
                           extra + "}"),
                     Config(), Access(), 42, true);
}
ResponseDelta Text(std::string text) {
  ResponseDelta delta;
  delta.kind = ResponseDelta::Kind::kContent;
  delta.text = std::move(text);
  return delta;
}
ObjectReply Stored(ObjectStore& objects, const std::string& id,
                   ObjectKind kind = ObjectKind::kResponse) {
  ObjectRequest request;
  request.scope = Access().scope;
  request.id = id;
  request.kind = kind;
  return objects.Execute(request, Clock::now() + 5s);
}
}  // namespace

Q4T_TEST(responses_background_queued_transition_is_durable_before_generation) {
  Fixture f;
  ObjectStore objects(f.options);
  ResponsesExecution run(objects);
  Q4T_CHECK(Ok(Prepare(run)));
  std::string wire;
  Q4T_CHECK(Ok(run.Begin(&wire)));
  Q4T_CHECK(wire.find("response.queued") != wire.npos);
  Q4T_CHECK(wire.find("response.in_progress") == wire.npos);
  Q4T_CHECK(!run.Append(Text("must not run yet"), "null", &wire));
  Q4T_CHECK(!run.Finish(false, 0, {1, 0, 1, 0}, 43, &wire).ok());
  Q4T_CHECK(ResponsesState(objects)
                .Retrieve(run.Id(), Access())
                .value.GetString("status") == "queued");
  Q4T_CHECK(Ok(run.StartRunning(&wire)));
  Q4T_CHECK(wire.find("response.in_progress") != wire.npos);
  Q4T_CHECK(wire.find("\"sequence_number\":2") != wire.npos);
  Q4T_CHECK(ResponsesState(objects)
                .Retrieve(run.Id(), Access())
                .value.GetString("status") == "in_progress");
  Q4T_CHECK(!run.StartRunning(&wire).ok());
  Q4T_CHECK(run.Append(Text("actual answer"), "null", &wire));
  Q4T_CHECK(Ok(run.Finish(false, 0, {5, 0, 2, 0}, 43, &wire)));
  Q4T_CHECK(Ok(run.Cancel()));
  Q4T_CHECK(run.Output().Snapshot().GetString("status") == "completed");
  return true;
}

Q4T_TEST(responses_background_cancel_preserves_partial_without_tool_done) {
  for (const bool started : {false, true}) {
    Fixture f;
    ObjectStore objects(f.options);
    ResponsesExecution run(objects);
    Q4T_CHECK(Ok(Prepare(run)));
    std::string wire;
    Q4T_CHECK(Ok(run.Begin(&wire)));
    if (started) {
      Q4T_CHECK(Ok(run.StartRunning(&wire)));
      ResponseDelta call;
      call.kind = ResponseDelta::Kind::kTool;
      call.tool_start = true;
      call.call = {0, "call_partial", "lookup", "{\"path\":"};
      Q4T_CHECK(run.Append(call, "null", &wire));
    }
    run.BindCancellation(Clock::now() - 1s, [] { return true; });
    Q4T_CHECK(Ok(run.Cancel()));
    const auto saved = Stored(objects, run.Id());
    Q4T_CHECK(Ok(saved));
    const auto* response = saved.value.Find("data")->Find("response");
    Q4T_CHECK(response->GetString("status") == "cancelled");
    Q4T_CHECK(response->Find("error")->IsNull());
    if (started)
      Q4T_CHECK(response->GetArray("output")->array[0].GetString("arguments") ==
                "{\"path\":");
    Q4T_CHECK(Ok(run.Cancel()) && Ok(run.CloseUnfinished()));
    Q4T_CHECK(Stored(objects, run.Id()).value.GetInt("revision") ==
              saved.value.GetInt("revision"));
    ResponsesReadPlan plan;
    plan.stream = true;
    plan.id = run.Id();
    ResponsesReplay replay;
    Q4T_CHECK(Ok(ReadResponsesState(objects, plan, Access(), &replay)));
    Q4T_CHECK(replay.terminal);
    for (const auto* forbidden : {"response.cancelled", "arguments.done",
                                  "response.completed", "response.failed"})
      Q4T_CHECK(replay.wire.find(forbidden) == replay.wire.npos);
  }
  return true;
}

Q4T_TEST(responses_background_store_false_has_temporary_polling_state) {
  for (const bool conversation : {false, true}) {
    Fixture f;
    ObjectStore objects(f.options);
    std::string extra = R"(,"store":false)";
    if (conversation) {
      auto created =
          ConversationState(objects).Create(Json(), Parse("[]"), Access());
      Q4T_CHECK(Ok(created));
      extra += ",\"conversation\":\"" + created.value.GetString("id") + '"';
    }
    ResponsesExecution run(objects);
    Q4T_CHECK(Ok(Prepare(run, extra)));
    std::string wire;
    Q4T_CHECK(Ok(run.Begin(&wire)) && Ok(run.StartRunning(&wire)));
    Q4T_CHECK(run.Append(Text("answer"), "null", &wire));
    Q4T_CHECK(Ok(run.Finish(false, 0, {5, 0, 1, 0}, 43, &wire)));
    const auto saved = Stored(objects, run.Id());
    Q4T_CHECK(Ok(saved));
    const auto ttl =
        saved.value.GetInt("expires_at") - saved.value.GetInt("created_at");
    Q4T_CHECK(ttl >= 600 && ttl <= 601);
    const auto* response = saved.value.Find("data")->Find("response");
    Q4T_CHECK(!response->GetBool("store", true));
    const auto item =
        Stored(objects, response->GetArray("output")->array[0].GetString("id"),
               ObjectKind::kConversationItem);
    Q4T_CHECK(Ok(item));
    Q4T_CHECK(conversation ? item.value.GetInt("expires_at") == 0
                           : item.value.GetInt("expires_at") > 0);
  }
  return true;
}

Q4T_TEST(
    responses_background_restore_queued_keeps_identity_and_resolved_history) {
  Fixture f;
  std::string id, history, original_events;
  {
    ObjectStore objects(f.options);
    ResponsesExecution run(objects);
    Q4T_CHECK(Ok(Prepare(run)));
    std::string wire;
    Q4T_CHECK(Ok(run.Begin(&wire)));
    id = run.Id();
    const auto saved = Stored(objects, id);
    Q4T_CHECK(Ok(saved));
    history = OutputJson(*saved.value.Find("data")->Find("history"));
    original_events = OutputJson(*saved.value.Find("data")->Find("events"));
  }
  ObjectStore objects(f.options);
  ResponsesExecution restored(objects);
  Q4T_CHECK(Ok(restored.RestoreQueued(id, Config(), Access())));
  Q4T_CHECK(restored.Id() == id && restored.Begun());
  const auto unchanged = Stored(objects, id);
  Q4T_CHECK(OutputJson(*unchanged.value.Find("data")->Find("history")) ==
            history);
  Q4T_CHECK(OutputJson(*unchanged.value.Find("data")->Find("events")) ==
            original_events);
  Q4T_CHECK(unchanged.value.GetInt("revision") == 1);
  std::string wire;
  Q4T_CHECK(Ok(restored.StartRunning(&wire)));
  Q4T_CHECK(restored.Append(Text("resumed actual answer"), "null", &wire));
  Q4T_CHECK(Ok(restored.Finish(false, 0, {5, 0, 2, 0}, 43, &wire)));
  Q4T_CHECK(restored.Output().Snapshot().GetInt("created_at") == 42);
  return true;
}

Q4T_TEST(responses_background_interrupted_execution_fails_without_replay) {
  Fixture f;
  std::string id;
  {
    ObjectStore objects(f.options);
    ResponsesExecution run(objects);
    Q4T_CHECK(Ok(Prepare(run)));
    std::string wire;
    Q4T_CHECK(Ok(run.Begin(&wire)) && Ok(run.StartRunning(&wire)));
    Q4T_CHECK(run.Append(Text("actual partial"), "null", &wire));
    Q4T_CHECK(Ok(run.Checkpoint()));
    id = run.Id();
  }
  ObjectStore objects(f.options);
  ResponsesExecution rejected(objects);
  Q4T_CHECK(rejected.RestoreQueued(id, Config(), Access()).code ==
            "response_not_queued");
  ResponsesState state(objects);
  const auto recovered = state.Interrupt(id, Access(), "process_interrupted",
                                         "Generation process exited");
  Q4T_CHECK(Ok(recovered) && recovered.value.GetString("status") == "failed");
  Q4T_CHECK(recovered.value.GetArray("output")
                ->array[0]
                .GetArray("content")
                ->array[0]
                .GetString("text") == "actual partial");
  const auto revision = Stored(objects, id).value.GetInt("revision");
  Q4T_CHECK(Ok(state.Interrupt(id, Access(), "repeat", "must not replace")));
  Q4T_CHECK(Stored(objects, id).value.GetInt("revision") == revision);
  ResponsesReadPlan plan;
  plan.id = id;
  plan.stream = true;
  ResponsesReplay replay;
  Q4T_CHECK(Ok(ReadResponsesState(objects, plan, Access(), &replay)) &&
            replay.terminal);
  Q4T_CHECK(replay.wire.find("process_interrupted") != replay.wire.npos);
  Q4T_CHECK(replay.wire.find("response.completed") == replay.wire.npos);
  return true;
}

Q4T_TEST(
    responses_background_changed_conversation_does_not_block_failure_closure) {
  Fixture f;
  ObjectStore objects(f.options);
  ConversationState conversations(objects);
  auto created = conversations.Create(Json(), Parse("[]"), Access());
  Q4T_CHECK(Ok(created));
  const auto conversation_id = created.value.GetString("id");
  ResponsesExecution run(objects);
  Q4T_CHECK(Ok(Prepare(run, ",\"conversation\":\"" + conversation_id + '"')));
  std::string wire;
  Q4T_CHECK(Ok(run.Begin(&wire)));
  Q4T_CHECK(Ok(conversations.Update(conversation_id,
                                    Parse(R"({"topic":"new"})"), Access())));
  ResponsesExecution restored(objects);
  Q4T_CHECK(restored.RestoreQueued(run.Id(), Config(), Access()).code ==
            "conversation_revision_conflict");
  Q4T_CHECK(Ok(
      ResponsesState(objects).Interrupt(run.Id(), Access(), "history_changed",
                                        "Cannot resume changed conversation")));
  Q4T_CHECK(conversations.ListItems(conversation_id, {}, Access())
                .value.GetArray("data")
                ->array.empty());
  return true;
}

Q4T_TEST(responses_background_restoration_cannot_cross_authenticated_scope) {
  Fixture f;
  ObjectStore objects(f.options);
  ResponsesExecution run(objects), other(objects);
  Q4T_CHECK(Ok(Prepare(run)));
  std::string wire;
  Q4T_CHECK(Ok(run.Begin(&wire)));
  Q4T_CHECK(other.RestoreQueued(run.Id(), Config(), Access("client:other"))
                .http_status == 404);
  Q4T_CHECK(ResponsesState(objects)
                .Interrupt(run.Id(), Access("client:other"), "wrong", "wrong")
                .http_status == 404);
  Q4T_CHECK(ResponsesState(objects)
                .Retrieve(run.Id(), Access())
                .value.GetString("status") == "queued");
  Q4T_CHECK(Ok(run.Cancel()));
  return true;
}

Q4T_TEST(responses_background_lost_start_receipt_never_starts_or_overwrites) {
  struct Reset {
    ~Reset() { ObjectWorkerEntryForTesting({}); }
  } reset;
  ObjectWorkerEntryForTesting("--q4t-object-worker-second-put-lost-ack");
  Fixture f;
  f.options.io_timeout_ms = 250;
  ObjectStore objects(f.options);
  ResponsesExecution run(objects);
  Q4T_CHECK(Ok(Prepare(run)));
  std::string wire;
  Q4T_CHECK(Ok(run.Begin(&wire)));
  auto result = run.StartRunning(&wire);
  Q4T_CHECK(result.commit_unknown && run.CommitUnknown() && wire.empty());
  Q4T_CHECK(run.Output().Snapshot().GetString("status") == "queued");
  Q4T_CHECK(!run.Append(Text("never execute"), "null", &wire));
  Q4T_CHECK(!run.Cancel().ok() && Ok(run.CloseUnfinished()));
  const auto deadline = Clock::now() + 500ms;
  while (objects.Health().pending && Clock::now() < deadline)
    std::this_thread::sleep_for(1ms);
  Q4T_CHECK(ResponsesState(objects)
                .Retrieve(run.Id(), Access())
                .value.GetString("status") == "in_progress");
  return true;
}
