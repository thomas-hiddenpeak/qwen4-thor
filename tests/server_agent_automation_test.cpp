#include "../src/server/agent_automation_internal.h"
#include "../src/server/agent_automation_tool.h"
#include "q4t/server/agent_session.h"
#include "../src/server/object_protocol.h"
#include "q4t/server/object_retention.h"
#include "q4t/server/responses_jobs.h"
#include "q4t/test.h"

#include <chrono>
#include <cstdlib>
#include <filesystem>

namespace q4t::server {
namespace {
using namespace object_detail;
using namespace automation_detail;
using namespace std::chrono_literals;
constexpr char kBase[] = "/v1/agents/automations";
constexpr int64_t kNow = 1800000000;

StateAccess Access(std::string scope = "client:a") {
  return {std::move(scope), std::chrono::steady_clock::now() + 10s, {}};
}

struct Fixture {
  ObjectStoreOptions options;
  ChatPlanConfig config;
  Fixture() {
    const char* output = std::getenv("Q4T_TEST_OUTPUT_DIR");
    const auto parent = std::filesystem::absolute(
        output ? output : ".q4t-work/automation-tests");
    std::filesystem::create_directories(parent);
    auto name = (parent / "automation-XXXXXX").string();
    if (!mkdtemp(name.data())) throw std::runtime_error("automation fixture");
    options.directory = name + "/store";
    options.restart_backoff_ms = 0;
    config.model = "cpu";
  }
  ObjectReply Apply(ObjectStore& db, const std::string& path,
                    const std::string& method, const Json& json,
                    int64_t now = kNow) {
    return Registry(db, Access(), config, ParseAutomationRoute(path), method,
                    json, now);
  }
};
Json CreateRequest(bool once = false) {
  const auto text =
      Object({{"type", String("input_text")}, {"text", String("Answer 42")}});
  auto target =
      Object({{"type", String("new_session")},
              {"agent", Object({{"model", String("cpu")}})},
              {"environment", Object({{"type", String("none")}})},
              {"input", Array({Object({{"role", String("user")},
                                       {"content", Array({text})}})})}});
  auto value = Object({{"type", String(once ? "once" : "periodic")},
                       {"name", String("project check")},
                       {"target", std::move(target)}});
  if (once) {
    Set(&value, "run_at", Number(kNow + 5));
  } else {
    Set(&value, "timezone", String("UTC"));
    Set(&value, "expression", String("* * * * *"));
  }
  return value;
}
ObjectReply Read(ObjectStore& db, const std::string& id, ObjectKind kind) {
  ObjectRequest request;
  request.scope = "client:a";
  request.id = id;
  request.kind = kind;
  return db.Execute(request, Access().deadline);
}
bool Ok(const ObjectReply& reply) {
  if (!reply.ok())
    std::fprintf(stderr, "%s %s %s\n", reply.code.c_str(),
                 reply.message.c_str(), reply.value.GetString("param").c_str());
  return reply.ok();
}

Q4T_TEST(agent_automation_schedule_is_atomic_across_restart_and_skips_pending) {
  Fixture f;
  std::string id, first, manual;
  {
    ObjectStore db(f.options);
    auto created = f.Apply(db, kBase, "POST", CreateRequest());
    Q4T_CHECK(Ok(created) && created.http_status == 201);
    id = created.value.GetString("id");
    auto row = Read(db, id, ObjectKind::kAutomation);
    Q4T_CHECK(Ok(row));
    auto due = Schedule(db, Access(), row.value, kNow + 60);
    Q4T_CHECK(Ok(due) && due.value.GetString("status") == "queued");
    first = due.value.GetString("id");
    Q4T_CHECK(Schedule(db, Access(), row.value, kNow + 60).http_status == 409);
  }
  ObjectStore db(f.options);
  auto current = Read(db, id, ObjectKind::kAutomation);
  Q4T_CHECK(Ok(current));
  auto retry = Schedule(db, Access(), current.value, kNow + 60);
  Q4T_CHECK(Ok(retry) && !retry.value.Has("id"));
  auto next = Schedule(db, Access(), current.value, kNow + 120);
  Q4T_CHECK(Ok(next) &&
            next.value.GetString("skip_reason") == "pending_delivery");
  auto path = std::string(kBase) + "/" + id;
  Q4T_CHECK(Ok(f.Apply(db, path + "/pause", "POST", Object())));
  current = Read(db, id, ObjectKind::kAutomation);
  auto invoked = Queue(db, Access(), current.value, kNow + 130, 0);
  Q4T_CHECK(Ok(invoked) && invoked.value.GetString("status") == "queued");
  manual = invoked.value.GetString("id");
  Q4T_CHECK(manual != first);
  current = Read(db, id, ObjectKind::kAutomation);
  Q4T_CHECK(Field(current.value, "data").GetString("pending_delivery") ==
            first);
  Q4T_CHECK(!Schedule(db, Access(), current.value, kNow + 180).value.Has("id"));
  // Route borrows the URI, so keep its backing string alive.
  const auto listing = path + "/invocations";
  auto page =
      List(db, Access(), ParseAutomationRoute(listing), "limit=1&order=asc");
  Q4T_CHECK(Ok(page) && page.value.GetBool("has_more") &&
            Field(page.value, "data").array.size() == 1);
  Q4T_CHECK(List(db, Access("client:b"), ParseAutomationRoute(listing), "")
                .http_status == 404);
  Q4T_CHECK(Ok(f.Apply(db, path + "/resume", "POST", Object(), kNow + 180)));
  Q4T_CHECK(Ok(f.Apply(db, path, "DELETE", Object())));
  auto queued = Read(db, manual, ObjectKind::kAutomationInvocation);
  Q4T_CHECK(Ok(queued) && Field(queued.value, "data").Has("sealed_target"));
  return true;
}

Q4T_TEST(agent_automation_session_ownership_and_atomic_conflict) {
  Fixture f;
  ObjectStore db(f.options);
  ObjectRequest session;
  session.operation = ObjectOperation::kPut;
  session.kind = ObjectKind::kAgentSession;
  session.scope = Access().scope;
  session.id = "agent_session_owner";
  session.data = Object();
  Q4T_CHECK(Ok(db.Execute(session, Access().deadline)));
  const auto create =
      Object({{"action", String("create")},
              {"type", String("once")},
              {"name", String("Release")},
              {"delay_seconds", Number(45)},
              {"instructions", String("Return the project revision")}});
  SessionAutomationChange caller{session.id, "call_owner", {}};
  auto apply = [&](const Json& op, SessionAutomationChange* actor,
                   bool dispatch = true) {
    return PlanSessionTool(db, Access(), f.config, op, kNow, dispatch, actor);
  };
  Q4T_CHECK(apply(create, &caller, false).http_status == 503 && !caller.write);
  auto invalid = create;
  Set(&invalid, "delay_seconds", Number(-1));
  Q4T_CHECK(apply(invalid, &caller).http_status == 400);
  Set(&invalid, "session_id", String("agent_session_other"));
  Q4T_CHECK(apply(invalid, &caller).http_status == 400 && !caller.write);
  auto planned = apply(create, &caller);
  Q4T_CHECK(Ok(planned) && caller.write);
  const auto id = planned.value.GetString("id");
  Q4T_CHECK(planned.value.GetInt("run_at") == kNow + 45 &&
            Read(db, id, ObjectKind::kAutomation).http_status == 404);
  auto write = std::move(*caller.write);
  caller.write.reset();
  Q4T_CHECK(Ok(db.Execute(write, Access().deadline)));
  auto other = SessionAutomationChange{"agent_session_other", "call_peer", {}};
  auto get = Object({{"action", String("get")}, {"automation_id", String(id)}});
  for (const char* action : {"get", "update", "pause", "resume", "delete"}) {
    Set(&get, "action", String(action));
    Q4T_CHECK(apply(get, &other).http_status == 404 && !other.write);
  }
  auto list = Object({{"action", String("list")}});
  Q4T_CHECK(apply(list, &other).value.GetArray("data")->array.empty());
  Set(&list, "after", String(id));
  Q4T_CHECK(apply(list, &other).http_status >= 400);
  auto all = f.Apply(db, std::string(kBase) + "/" + id, "GET", Object());
  Q4T_CHECK(Ok(all) &&
            all.value.Find("created_by")->GetString("session_id") ==
                session.id &&
            all.value.Find("created_by")->GetString("tool_call_id") ==
                caller.call_id);
  auto target = *all.value.Find("target");
  Set(&target, "session_id", String(other.session));
  Q4T_CHECK(f.Apply(db, std::string(kBase) + "/" + id, "POST",
                    Object({{"target", target}}))
                .http_status == 400);
  auto dev = f.Apply(db, kBase, "POST", CreateRequest());
  Q4T_CHECK(Ok(dev));
  Set(&get, "automation_id", *dev.value.Find("id"));
  Q4T_CHECK(apply(get, &caller).http_status == 404 && !caller.write);
  Set(&get, "automation_id", String(id));
  Q4T_CHECK(Ok(apply(get, &caller)) && caller.write);
  Q4T_CHECK(Ok(
      f.Apply(db, std::string(kBase) + "/" + id + "/pause", "POST", Object())));
  ResponseStatePlan plan;
  plan.scope = session.scope;
  plan.id = "resp_automation_conflict";
  plan.agent_session_id = session.id;
  plan.request = Object();
  plan.history = Array();
  plan.own_input = Array();
  plan.response_revision = 1;
  auto snapshot = Object({{"id", String(plan.id)},
                          {"status", String("in_progress")},
                          {"output", Array()}});
  ObjectRequest response;
  response.operation = ObjectOperation::kPut;
  response.kind = ObjectKind::kResponse;
  response.scope = plan.scope;
  response.id = plan.id;
  response.data = Object();
  Q4T_CHECK(Ok(db.Execute(response, Access().deadline)));
  auto failed = ResponsesState(db).Checkpoint(snapshot, Array(), Access(),
                                              &plan, &*caller.write);
  Q4T_CHECK(!failed.ok() && !failed.commit_unknown &&
            plan.response_revision == 1);
  auto row = Read(db, plan.id, ObjectKind::kResponse);
  Q4T_CHECK(Ok(row) && row.value.GetInt("revision") == 1 &&
            row.value.Find("data")->object.empty());
  auto survivor = Read(db, id, ObjectKind::kAutomation);
  Q4T_CHECK(
      Ok(survivor) &&
      survivor.value.Find("data")->Find("resource")->GetString("status") ==
          "paused");
  return true;
}

Q4T_TEST(agent_automation_tool_durable_roundtrip_and_client_name_isolation) {
  Fixture f;
  f.config.structured = true;
  std::string id, scheduled, call_id = "call_wakeup";
  Json request;
  Q4T_CHECK(io::ParseJson(
      R"({"agent":{"model":"cpu","tools":[{"type":"automations"}]},
    "environment":{"type":"none"},"input":"Set a wakeup"})",
      &request, true));
  {
    ObjectStore db(f.options);
    ResponsesExecution run(db);
    const auto now = std::time(nullptr);
    auto start =
        PrepareAgentSessionExecution(db, request, f.config, Access(), now, run);
    Q4T_CHECK(Ok(start) && run.HasNativeTools());
    const auto& model_tool =
        run.Generation().generation_request.GetArray("tools")->array.front();
    const auto* fn = model_tool.Find("function");
    Q4T_CHECK(fn && !fn->GetBool("strict") &&
              fn->Find("parameters")->GetArray("required")->array.size() == 1);
    id = start.value.GetString("id");
    run.BindBackgroundControl(
        std::make_shared<RequestControl>(run.Id(), "", Access().deadline));
    std::string wire;
    Q4T_CHECK(Ok(run.Begin(&wire)) && Ok(run.StartRunning(&wire)));
    ResponseDelta delta;
    delta.kind = ResponseDelta::Kind::kTool;
    delta.call = {0, call_id, kToolName,
                  R"({"action":"create","name":"Release",)"};
    Q4T_CHECK(run.Append(delta, "null", &wire) && wire.empty() &&
              run.PendingAutomations());
    delta.tool_start = false;
    delta.call = {
        0, "", "",
        R"("instructions":"Return 469","type":"once","delay_seconds":600})"};
    Q4T_CHECK(run.Append(delta, "null", &wire));
    unsigned woke = 0;
    bool published = false;
    Q4T_CHECK(Ok(run.RunSessionAutomations(
                  1, f.config, true, [&] { ++woke; },
                  [&](const std::string& journal) {
                    published = journal.find("function_call_output") !=
                                std::string::npos;
                    return true;
                  })) &&
              woke == 1 && published && !run.PendingAutomations());
    ResponsesGenerationPlan cont;
    Q4T_CHECK(Ok(run.ContinueFileSearch(2, f.config, {}, &cont)));
    ResponseDelta answer;
    answer.kind = ResponseDelta::Kind::kContent;
    answer.text = "Scheduled";
    Q4T_CHECK(run.Append(answer, "null", &wire));
    Q4T_CHECK(Ok(run.Finish(false, 0, {9, 0, 12, 0}, now, &wire)));
    AgentSessionView view;
    Q4T_CHECK(Ok(ReadAgentSessionTurn(db, id, "", Access(), &view)) &&
              view.session.GetString("status") == "idle" &&
              view.items.array.size() == 4 &&
              view.items.array[1].GetString("call_id") == call_id &&
              view.items.array[2].GetString("call_id") == call_id);
    Json result;
    Q4T_CHECK(
        io::ParseJson(view.items.array[2].GetString("output"), &result, true));
    scheduled = result.GetString("id");
    auto stored = Read(db, scheduled, ObjectKind::kAutomation);
    Q4T_CHECK(Ok(stored) && stored.value.Find("data")
                                    ->Find("resource")
                                    ->Find("created_by")
                                    ->GetString("tool_call_id") == call_id);
  }
  ObjectStore db(f.options);
  AgentEventCursor cursor;
  Json events;
  bool settled = false;
  Q4T_CHECK(Ok(
      ReplayAgentSessionEvents(db, id, Access(), &cursor, &events, &settled)));
  size_t calls = 0, outputs = 0;
  for (const auto& event : events.array) {
    const auto* item = event.Find("item");
    if (!item || item->GetString("call_id") != call_id) continue;
    if (item->GetString("type") == "function_call") {
      Q4T_CHECK(event.GetString("type") == "agent.session.turn.item.done");
      ++calls;
    } else {
      Q4T_CHECK(event.GetString("type") == "agent.session.turn.item.added" &&
                event.Find("output_index")->IsNull());
      ++outputs;
    }
  }
  Q4T_CHECK(calls == 1 && outputs == 1 &&
            Ok(Read(db, scheduled, ObjectKind::kAutomation)));
  auto external = SessionTool();
  std::erase_if(external.object,
                [](const auto& field) { return field.first == "strict"; });
  auto& tools = Field(Field(request, "agent"), "tools");
  tools.array.push_back(external);
  ResponsesExecution duplicate(db);
  Q4T_CHECK(PrepareAgentSessionExecution(db, request, f.config, Access(),
                                         std::time(nullptr), duplicate)
                .http_status == 400);
  tools = Array({external});
  ResponsesExecution unmanaged(db);
  auto start = PrepareAgentSessionExecution(db, request, f.config, Access(),
                                            std::time(nullptr), unmanaged);
  Q4T_CHECK(Ok(start) && !unmanaged.HasNativeTools());
  unmanaged.BindBackgroundControl(
      std::make_shared<RequestControl>(unmanaged.Id(), "", Access().deadline));
  std::string wire;
  Q4T_CHECK(Ok(unmanaged.Begin(&wire)) && Ok(unmanaged.StartRunning(&wire)));
  ResponseDelta delta;
  delta.kind = ResponseDelta::Kind::kTool;
  delta.call = {0, "external", kToolName, R"({"action":"list"})"};
  Q4T_CHECK(unmanaged.Append(delta, "null", &wire) &&
            !unmanaged.PendingAutomations());
  Q4T_CHECK(
      Ok(unmanaged.Finish(false, 1, {6, 0, 2, 0}, std::time(nullptr), &wire)));
  AgentSessionView view;
  Q4T_CHECK(Ok(ReadAgentSessionTurn(db, start.value.GetString("id"), "",
                                    Access(), &view)) &&
            view.session.GetString("status") == "requires_action");
  return true;
}
Q4T_TEST(agent_automation_once_pause_update_and_complete) {
  Fixture f;
  ObjectStore db(f.options);
  auto body = CreateRequest(true);
  auto created = f.Apply(db, kBase, "POST", body);
  Q4T_CHECK(Ok(created));
  const auto id = created.value.GetString("id");
  const auto path = std::string(kBase) + "/" + id;
  Q4T_CHECK(Ok(f.Apply(db, path + "/pause", "POST", Object())));
  Q4T_CHECK(
      f.Apply(db, path + "/resume", "POST", Object(), kNow + 15).http_status ==
      400);
  Q4T_CHECK(Ok(f.Apply(db, path, "POST",
                       Object({{"run_at", Number(kNow + 40)}}), kNow + 15)));
  Q4T_CHECK(Ok(f.Apply(db, path + "/resume", "POST", Object(), kNow + 15)));
  auto current = Read(db, id, ObjectKind::kAutomation);
  Q4T_CHECK(Ok(current) &&
            !Schedule(db, Access(), current.value, kNow + 39).value.Has("id"));
  auto due = Schedule(db, Access(), current.value, kNow + 40);
  Q4T_CHECK(Ok(due) && due.value.GetInt("scheduled_at") == kNow + 40);
  auto done = f.Apply(db, path, "GET", Object(), kNow + 41);
  Q4T_CHECK(Ok(done) && done.value.GetString("status") == "completed");
  Q4T_CHECK(f.Apply(db, path, "POST", Object({{"run_at", Number(kNow + 60)}}))
                .http_status == 400);
  return true;
}
Q4T_TEST(
    agent_automation_delivery_waits_for_capacity_and_closes_missing_target) {
  Fixture f;
  ObjectStore db(f.options);
  ClientAdmission clients;
  GatewayClientPolicy policy;
  const auto token = std::string(64, 'a');
  const auto raw =
      R"({"version":1,"gateway_token":")" + token +
      R"(","clients":[{"id":"a","max_inflight":1,"max_control_inflight":2,)"
      R"("max_prompt_tokens":4096,"max_output_tokens":256,"max_body_bytes":1048576}]})";
  Q4T_CHECK(ParseGatewayClientPolicy(raw, &policy));
  Q4T_CHECK(clients.Configure(policy));
  const auto client = clients.Authenticate("a", policy.gateway_token);
  Q4T_CHECK(client.Allowed());
  ResponsesJobs jobs(db, {}, [](auto&) {
    throw std::runtime_error("deleted session reached generation");
  });
  ObjectRequest session;
  session.operation = ObjectOperation::kPut;
  session.kind = ObjectKind::kAgentSession;
  session.scope = "client:a";
  session.id = "agent_session_existing";
  session.data = Object();
  Q4T_CHECK(Ok(db.Execute(session, Access().deadline)));
  auto request = CreateRequest(true);
  Set(&request, "target",
      Object({{"type", String("existing_session")},
              {"session_id", String(session.id)},
              {"instructions", String("Read the release")}}));
  auto created = f.Apply(db, kBase, "POST", request);
  Q4T_CHECK(Ok(created));
  const auto parent =
      Read(db, created.value.GetString("id"), ObjectKind::kAutomation);
  auto queued = Queue(db, Access(), parent.value, kNow, 0);
  Q4T_CHECK(Ok(queued));
  const auto id = queued.value.GetString("id");
  auto pending = Read(db, id, ObjectKind::kAutomationInvocation);
  Q4T_CHECK(Ok(pending));
  Q4T_CHECK(Deliver(db, clients, client, jobs, f.config, Access("client:b"),
                    pending.value, kNow)
                .http_status == 403);
  {
    ClientLease held;
    Q4T_CHECK(held.Acquire(clients, client, ClientLane::kData));
    Q4T_CHECK(Ok(Deliver(db, clients, client, jobs, f.config, Access(),
                         pending.value, kNow)));
  }
  auto delayed = Read(db, id, ObjectKind::kAutomationInvocation);
  Q4T_CHECK(Ok(delayed));
  const auto& data = Field(delayed.value, "data");
  Q4T_CHECK(data.GetInt("next_attempt") > kNow && data.Has("sealed_target"));
  Q4T_CHECK(data.Find("resource")->GetString("status") == "queued");
  session.operation = ObjectOperation::kErase;
  Q4T_CHECK(Ok(db.Execute(session, Access().deadline)));
  Q4T_CHECK(Ok(Deliver(db, clients, client, jobs, f.config, Access(),
                       delayed.value, data.GetInt("next_attempt"))));
  auto terminal = Read(db, id, ObjectKind::kAutomationInvocation);
  Q4T_CHECK(Ok(terminal));
  const auto& saved = Field(terminal.value, "data");
  Q4T_CHECK(!saved.Has("sealed_target") && !saved.Has("target_references"));
  const auto& resource = *saved.Find("resource");
  Q4T_CHECK(resource.GetString("status") == "failed");
  Q4T_CHECK(resource.Find("error")->GetString("code") == "object_not_found");
  return true;
}
Q4T_TEST(agent_automation_hosted_projection_keeps_private_target_sealed) {
  Fixture f;
  ObjectStore db(f.options);
  auto request = CreateRequest(true);
  auto& target = Field(request, "target");
  Json env;
  Q4T_CHECK(io::ParseJson(R"({"type":"openai_hosted",
    "env":{"API_TOKEN":"sealed-environment-value"},
    "setup_commands":[{"command":"sealed-setup-command"}],
    "files":[{"type":"inline","path":"/workspace/release","data":"SGVsbG8="}]})",
                          &env, true));
  Set(&target, "environment", std::move(env));
  auto created = f.Apply(db, kBase, "POST", request);
  Q4T_CHECK(Ok(created));
  auto stored =
      Read(db, created.value.GetString("id"), ObjectKind::kAutomation);
  Q4T_CHECK(Ok(stored));
  auto queued = Queue(db, Access(), stored.value, kNow, 0);
  Q4T_CHECK(Ok(queued));
  auto snapshot =
      Read(db, queued.value.GetString("id"), ObjectKind::kAutomationInvocation);
  Q4T_CHECK(Ok(snapshot));
  for (const auto* object : {&created.value, &stored.value, &snapshot.value}) {
    const auto wire = OutputJson(*object);
    for (const auto* value :
         {"sealed-environment-value", "sealed-setup-command", "SGVsbG8="})
      Q4T_CHECK(wire.find(value) == std::string::npos);
  }
  const auto& selection = Field(Field(created.value, "target"), "environment");
  const auto* files = selection.GetArray("files");
  Q4T_CHECK(files && files->array.size() == 1 &&
            files->array[0].GetInt("size_bytes") == 5);
  return true;
}
}  // namespace
}  // namespace q4t::server
