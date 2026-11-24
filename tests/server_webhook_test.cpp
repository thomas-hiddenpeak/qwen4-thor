#include "../src/server/object_protocol.h"
#include "../src/server/webhook_keys.h"
#include "../src/server/webhook_events.h"
#include "q4t/server/webhook.h"
#include "q4t/server/agent_session.h"
#include "q4t/server/agent_environment.h"
#include "q4t/server/server_options.h"
#include "q4t/server/object_retention.h"
#include "q4t/test.h"

#include <array>
#include <chrono>
#include <cstdlib>
#include <filesystem>
#include <set>
#include <thread>

namespace q4t::server {
namespace {
using namespace object_detail;
using namespace std::chrono_literals;
constexpr auto kEndpoints = "/v1/webhook_endpoints";
constexpr auto kCreate =
    R"({"name":"Builds","url":"https://subscriber.test/events","event_types":["response.completed"]})";

StateAccess Access(std::string scope = "client:one") {
  return {std::move(scope), std::chrono::steady_clock::now() + 10s, {}};
}
struct Fixture {
  std::filesystem::path directory;
  ObjectStoreOptions options;
  FileFetchOptions policy;
  Fixture() {
    const char* base = std::getenv("Q4T_TEST_OUTPUT_DIR");
    directory =
        std::filesystem::absolute(base ? base : ".q4t-work/webhook-tests");
    std::filesystem::create_directories(directory);
    auto pattern = (directory / "subscriber-XXXXXX").string();
    if (!mkdtemp(pattern.data())) throw std::runtime_error("webhook fixture");
    directory = pattern;
    options.directory = (directory / "objects").string();
    policy.allowed_hosts = {"subscriber.test"};
    policy.allow_http = false;
  }
  ~Fixture() {
    std::error_code ec;
    std::filesystem::remove_all(directory, ec);
  }
  ObjectReply Call(ObjectStore& db, std::string_view verb, std::string_view url,
                   const std::string& body = "") {
    return HandleWebhook(db, Access(), &policy, verb, url, body);
  }
};

Q4T_TEST(webhook_deployment_policy_reaches_serve_options) {
  const std::array<std::string_view, 2> args{"--webhook-policy-file",
                                             "/subscriber.json"};
  ServerOptions options;
  options.model_dir = "/read-only-model";
  Q4T_CHECK(ParseServerOptions(args, &options).ok());
  Q4T_CHECK(options.webhook_policy_file == "/subscriber.json");
  Q4T_CHECK(!ParseServerOptions(std::span(args).first(1), &options).ok());
  return true;
}

Q4T_TEST(webhook_endpoint_owner_projection_and_rotation) {
  Fixture f;
  std::string id, first_secret;
  {
    ObjectStore db(f.options);
    auto created = f.Call(db, "POST", kEndpoints, kCreate);
    Q4T_CHECK(created.ok() && created.http_status == 200);
    auto& item = created.value;
    id = item.GetString("id");
    first_secret = item.GetString("signing_secret");
    Q4T_CHECK(id.starts_with("whe_") && first_secret.starts_with("whsec_") &&
              first_secret.size() == 50);
  }
  ObjectStore db(f.options);
  auto path = std::string(kEndpoints) + "/" + id;
  const auto get = f.Call(db, "GET", path);
  Q4T_CHECK(get.ok() && !get.value.Has("signing_secret"));
  Q4T_CHECK(HandleWebhook(db, Access("client:peer"), &f.policy, "GET", path, "")
                .http_status == 404);
  auto list = f.Call(db, "GET", std::string(kEndpoints) + "?limit=1");
  Q4T_CHECK(list.ok() && list.value.GetArray("data")->array.size() == 1);
  Q4T_CHECK(OutputJson(list.value).find(first_secret) == std::string::npos);
  const auto same = f.Call(db, "POST", path, R"({"name":"Builds"})");
  Q4T_CHECK(same.ok() && OutputJson(same.value) == OutputJson(get.value));
  auto rotated = f.Call(db, "POST", path + "/rotate_secret",
                        R"({"keep_old_secret_active_for_24_hours":true})");
  Q4T_CHECK(rotated.ok() &&
            rotated.value.GetString("signing_secret") != first_secret);
  ObjectRequest req;
  req.scope = Access().scope;
  req.kind = ObjectKind::kWebhookEndpoint;
  req.id = id;
  auto row = db.Execute(req, Access().deadline);
  Q4T_CHECK(row.ok() &&
            OutputJson(row.value).find(first_secret) == std::string::npos);
  Json keys;
  auto now = std::time(nullptr);
  auto record = Field(row.value, "data");
  const auto retention = InspectObjectRetention("webhook_endpoint", record);
  Q4T_CHECK(retention.active && !retention.uncertain);
  Q4T_CHECK(InspectObjectRetention("webhook_endpoint", Object()).uncertain);

  Q4T_CHECK(
      webhook_detail::ReadKeys(db, Access(), id, record, now, &keys).ok());
  Q4T_CHECK(keys.array.size() == 2 &&
            keys.array[1].GetString("secret") == first_secret);
  auto expiry = keys.array[1].GetInt("expires_at");
  Q4T_CHECK(expiry >= now + 86390 && expiry <= now + 86400);
  Q4T_CHECK(
      webhook_detail::ReadKeys(db, Access(), id, record, expiry, &keys).ok());
  Q4T_CHECK(keys.array.size() == 1);
  auto erased = f.Call(db, "DELETE", path);
  Q4T_CHECK(erased.ok() && erased.value.GetBool("deleted"));
  Q4T_CHECK(f.Call(db, "GET", path).http_status == 404);
  return true;
}

Q4T_TEST(webhook_rejects_denied_target_and_bad_queries_without_network) {
  Fixture f;
  ObjectStore db(f.options);
  auto denied = f.Call(
      db, "POST", kEndpoints,
      R"({"name":"Builds","url":"https://unlisted.test","event_types":["response.completed"]})");
  Q4T_CHECK(denied.http_status == 403);
  auto invalid = f.Call(
      db, "POST", kEndpoints,
      R"({"name":"Bad","url":"http://subscriber.test","event_types":["response.completed"]})");
  Q4T_CHECK(invalid.http_status == 400);
  Q4T_CHECK(
      f.Call(db, "GET", std::string(kEndpoints) + "?order=asc").http_status ==
      400);
  Q4T_CHECK(
      f.Call(db, "GET", std::string(kEndpoints) + "?limit=0").http_status ==
      400);
  Q4T_CHECK(f.Call(db, "POST", "/v1/webhook_event_types", "{}").http_status ==
            405);
  Q4T_CHECK(
      f.Call(db, "GET", "/v1/webhook_event_types").value.GetArray("data"));
  auto item = f.Call(db, "POST", kEndpoints, kCreate);
  Q4T_CHECK(item.ok());
  auto unavailable = HandleWebhook(
      db, Access(), nullptr, "POST",
      std::string(kEndpoints) + "/" + item.value.GetString("id") + "/test",
      R"({"event_type":"response.completed"})");
  Q4T_CHECK(unavailable.http_status == 503 &&
            unavailable.code == "webhook_policy_unavailable");
  Q4T_CHECK(f.Call(db, "GET", kEndpoints).ok());
  return true;
}
ObjectRequest Subscription(std::string scope, std::string id,
                           std::string type) {
  ObjectRequest write;
  write.operation = ObjectOperation::kPut;
  write.kind = ObjectKind::kWebhookEndpoint;
  write.scope = std::move(scope);
  write.id = std::move(id);
  write.ttl_seconds = 0;
  write.data =
      Object({{"format", String("q4t.webhook.endpoint.v1")},
              {"sealed_keys", String("unused-in-outbox-fixture")},
              {"resource", Object({{"object", String("webhook_endpoint")},
                                   {"id", String(write.id)},
                                   {"event_types", Array({String(type)})}})}});
  return write;
}
ObjectRequest Background(std::string id, std::string status, int revision = 0) {
  ObjectRequest req;
  req.operation = ObjectOperation::kPut;
  req.kind = ObjectKind::kResponse;
  req.scope = Access().scope;
  req.id = std::move(id);
  req.expected_revision = revision;
  req.data =
      Object({{"response", Object({{"id", String(req.id)},
                                   {"object", String("response")},
                                   {"background", Boolean(true)},
                                   {"status", String(std::move(status))}})}});
  return req;
}
ObjectReply Events(ObjectStore& db, std::string scope = Access().scope) {
  ObjectRequest list;
  list.operation = ObjectOperation::kList;
  list.kind = ObjectKind::kWebhookEvent;
  list.scope = std::move(scope);
  return db.Execute(list, Access().deadline);
}

Q4T_TEST(
    webhook_terminal_event_commits_once_with_matching_owner_and_survives_delete) {
  Fixture f;
  {
    ObjectStore db(f.options);
    Q4T_CHECK(
        db.Execute(Subscription(Access().scope, "whe_a", "response.completed"),
                   Access().deadline)
            .ok());
    Q4T_CHECK(
        db.Execute(Subscription("client:peer", "whe_b", "response.completed"),
                   Access().deadline)
            .ok());
    Q4T_CHECK(
        db.Execute(Subscription(Access().scope, "whe_c", "batch.completed"),
                   Access().deadline)
            .ok());
    auto foreground = Background("resp_foreground", "completed");
    Set(&Field(foreground.data, "response"), "background", Boolean(false));
    Q4T_CHECK(db.Execute(foreground, Access().deadline).ok());
    Q4T_CHECK(
        db.Execute(Background("resp_work", "queued"), Access().deadline).ok());
    Q4T_CHECK(Events(db).value.GetArray("data")->array.empty());
    auto terminal = Background("resp_work", "completed", 1);
    auto stale = terminal;
    stale.expected_revision = 2;
    Q4T_CHECK(db.Execute(stale, Access().deadline).http_status == 409);
    Q4T_CHECK(Events(db).value.GetArray("data")->array.empty());
    auto committed = db.ExecuteBatch({&terminal, 1}, Access().deadline);
    Q4T_CHECK(committed.ok() &&
              committed.value.GetArray("data")->array.size() == 1);
    Q4T_CHECK(
        db.Execute(Background("resp_work", "completed", 2), Access().deadline)
            .ok());
    terminal.operation = ObjectOperation::kErase;
    terminal.expected_revision = 3;
    Q4T_CHECK(db.Execute(terminal, Access().deadline).ok());
  }
  ObjectStore reopened(f.options);
  auto result = Events(reopened);
  Q4T_CHECK(result.ok() && result.value.GetArray("data")->array.size() == 1);
  const auto& record =
      *result.value.GetArray("data")->array.front().Find("data");
  const auto* targets = record.GetArray("deliveries");
  Q4T_CHECK(targets && targets->array.size() == 1 &&
            targets->array.front().GetString("endpoint") == "whe_a");
  const auto& event = *record.Find("event");
  Q4T_CHECK(event.GetString("type") == "response.completed" &&
            event.Find("data")->GetString("id") == "resp_work");
  Q4T_CHECK(
      Events(reopened, "client:peer").value.GetArray("data")->array.empty());
  return true;
}

Q4T_TEST(
    webhook_count_quota_defers_without_breaking_background_and_recovers_on_reopen) {
  Fixture f;
  f.options.max_client_objects = 2;
  auto endpoint =
      Subscription(Access().scope, "whe_wait", "response.completed");
  {
    ObjectStore db(f.options);
    Q4T_CHECK(db.Execute(endpoint, Access().deadline).ok());
    Q4T_CHECK(
        db.Execute(Background("resp_full", "queued"), Access().deadline).ok());
    auto accepted =
        db.Execute(Background("resp_full", "completed", 1), Access().deadline);
    Q4T_CHECK(accepted.ok() && accepted.value.GetInt("revision") == 2);
    Q4T_CHECK(OutputJson(accepted.value).find("q4t_webhook_deferred") ==
              std::string::npos);
    Q4T_CHECK(Events(db).value.GetArray("data")->array.empty());
    auto diagnostic = db.ReconcileWebhooks("", Access().deadline);
    Q4T_CHECK(diagnostic.ok() &&
              diagnostic.value.GetInt("capacity_deferrals") == 1);
  }
  ObjectStore db(f.options);
  endpoint.operation = ObjectOperation::kErase;
  Q4T_CHECK(db.Execute(endpoint, Access().deadline).ok());
  Q4T_CHECK(db.ReconcileWebhooks("", Access().deadline).ok());
  auto page = Events(db);
  Q4T_CHECK(page.ok() && page.value.GetArray("data")->array.size() == 1);
  Q4T_CHECK(db.ReconcileWebhooks("", Access().deadline).ok());
  Q4T_CHECK(Events(db).value.GetArray("data")->array.size() == 1);
  return true;
}

Q4T_TEST(webhook_byte_limit_reports_loss_without_losing_the_response) {
  Fixture f;
  f.options.max_client_objects = 2;
  f.options.max_object_bytes = 1024;
  ObjectStore db(f.options);
  Q4T_CHECK(db.Execute(Subscription(Access().scope, "whe_bytes",
                                    "response.completed"),
                       Access().deadline)
                .ok());
  Q4T_CHECK(
      db.Execute(Background("resp_limit", "queued"), Access().deadline).ok());
  auto completed = Background("resp_limit", "completed", 1);
  Set(&completed.data, "metadata", String(std::string(430, 'x')));
  const auto accepted = db.Execute(completed, Access().deadline);
  Q4T_CHECK(accepted.ok() && accepted.value.GetInt("revision") == 2);
  Q4T_CHECK(OutputJson(*accepted.value.Find("data")) ==
            OutputJson(completed.data));
  Q4T_CHECK(Events(db).value.GetArray("data")->array.empty());
  auto diagnostic = db.ReconcileWebhooks("", Access().deadline);
  Q4T_CHECK(diagnostic.ok() && diagnostic.value.GetInt("capacity_losses") == 1);
  Q4T_CHECK(diagnostic.value.GetInt("capacity_deferrals") == 0);
  return true;
}

Q4T_TEST(webhook_agent_creations_project_only_public_routing_fields_once) {
  Fixture f;
  ObjectStore store(f.options);
  for (auto type : {"agent.session.created", "agent.environment.ready"}) {
    auto id =
        type == std::string_view("agent.session.created") ? "whe_s" : "whe_e";
    Q4T_CHECK(
        store.Execute(Subscription(Access().scope, id, type), Access().deadline)
            .ok());
  }
  ObjectRequest session;
  session.operation = ObjectOperation::kPut;
  session.kind = ObjectKind::kAgentSession;
  session.scope = Access().scope;
  session.id = "agent_session_demo";
  session.data =
      Object({{"session",
               Object({{"id", String(session.id)},
                       {"object", String("agent.session")},
                       {"environment",
                        Object({{"type", String("openai_hosted")},
                                {"id", String("env_warm")},
                                {"network", String("private-setting")}})},
                       {"metadata",
                        Object({{"private", String("sensitive-value")}})}})}});
  ObjectRequest env = session;
  env.id = "env_warm";
  env.kind = ObjectKind::kEnvironment;
  env.data =
      Object({{"resource", Object({{"id", String(env.id)},
                                   {"object", String("agent.environment")},
                                   {"type", String("openai_hosted")},
                                   {"status", String("ready")}})},
              {"warm_until", Number(std::time(nullptr) + 300)}});
  const std::array writes{env, session};
  auto created = store.ExecuteBatch(writes, Access().deadline);
  Q4T_CHECK(created.ok() && created.value.GetArray("data")->array.size() == 2);
  session.expected_revision = 1;
  Q4T_CHECK(store.Execute(session, Access().deadline).ok());
  env.expected_revision = 1;
  Q4T_CHECK(store.Execute(env, Access().deadline).ok());
  auto events = Events(store);
  Q4T_CHECK(events.ok() && events.value.GetArray("data")->array.size() == 2);
  for (const auto& row : events.value.GetArray("data")->array) {
    const auto& notice = *row.Find("data");
    const auto& event = *notice.Find("event");
    const auto& payload = *event.Find("data");
    const auto type = event.GetString("type");
    ChatPlanError error;
    Q4T_CHECK(ValidateWebhook(event, "event", &error).ok());
    auto sample = event;
    Set(&sample, "data", webhook_detail::SamplePayload(type, "example"));
    Q4T_CHECK(ValidateWebhook(sample, "event", &error).ok());
    const auto& deliveries = notice.GetArray("deliveries")->array;
    Q4T_CHECK(deliveries.size() == 1);
    if (type == "agent.session.created") {
      Q4T_CHECK(payload.object.size() == 3 &&
                payload.GetString("id") == session.id);
      Q4T_CHECK(payload.GetString("environment_type") == "openai_hosted" &&
                payload.GetString("environment_id") == env.id &&
                deliveries[0].GetString("endpoint") == "whe_s");
    } else {
      Q4T_CHECK(type == "agent.environment.ready" &&
                payload.object.size() == 1 &&
                payload.GetString("id") == env.id &&
                deliveries[0].GetString("endpoint") == "whe_e");
    }
    Q4T_CHECK(OutputJson(event).find("private") == std::string::npos &&
              OutputJson(event).find("sensitive") == std::string::npos);
  }
  return true;
}

Q4T_TEST(
    webhook_source_transition_filters_progress_and_connected_environments) {
  using webhook_detail::Transition;
  for (const auto kind : {"response", "batch"}) {
    for (const auto status : {"completed", "failed", "cancelled", "incomplete",
                              "expired", "in_progress"}) {
      auto source = Object({{kind, Object({{"background", Boolean(true)},
                                           {"status", String(status)}})}});
      const bool valid = status != std::string_view("in_progress") &&
                         !(kind == std::string_view("batch") &&
                           status == std::string_view("incomplete")) &&
                         !(kind == std::string_view("response") &&
                           status == std::string_view("expired"));
      const auto expected = valid ? std::string(kind) + "." + status : "";
      Q4T_CHECK(Transition(kind, source, false) == expected);
    }
  }
  const auto connected =
      Object({{"resource", Object({{"object", String("agent.environment")},
                                   {"type", String("openai_hosted")},
                                   {"status", String("connected")}})}});
  Q4T_CHECK(Transition("environment", connected, true).empty());
  auto other = connected;
  Set(&Field(other, "resource"), "status", String("ready"));
  Set(&Field(other, "resource"), "type", String("self_hosted"));
  Q4T_CHECK(Transition("environment", other, true).empty());
  const auto local =
      Object({{"session",
               Object({{"id", String("agent_session_x")},
                       {"environment", Object({{"type", String("none")}})}})}});
  const auto event = webhook_detail::EventPayload("agent.session.created",
                                                  "agent_session_x", local);
  Q4T_CHECK(event.object.size() == 2 &&
            event.GetString("environment_type") == "none");
  return true;
}

Q4T_TEST(webhook_reconciles_two_creation_events_one_free_slot_at_a_time) {
  Fixture f;
  f.options.max_client_objects = 3;
  auto created =
      Subscription(Access().scope, "whe_created", "agent.session.created");
  auto idle = Subscription(Access().scope, "whe_idle", "agent.session.idle");
  ObjectRequest session;
  session.kind = ObjectKind::kAgentSession;
  session.operation = ObjectOperation::kPut;
  session.scope = Access().scope;
  session.id = "agent_session_full";
  session.ttl_seconds = 0;
  session.data =
      Object({{"session",
               Object({{"object", String("agent.session")},
                       {"id", String(session.id)},
                       {"environment", Object({{"type", String("none")}})}})},
              {"created_idle", Boolean(true)},
              {"turns", Array()}});
  {
    ObjectStore db(f.options);
    Q4T_CHECK(db.Execute(created, Access().deadline).ok());
    Q4T_CHECK(db.Execute(idle, Access().deadline).ok());
    auto result = db.Execute(session, Access().deadline);
    Q4T_CHECK(result.ok() &&
              !result.value.Find("data")->Has("q4t_webhook_deferred"));
    Q4T_CHECK(Events(db).value.GetArray("data")->array.empty());
    auto counts = db.ReconcileWebhooks("", Access().deadline);
    Q4T_CHECK(counts.ok() && counts.value.GetInt("capacity_deferrals") == 2);
  }
  ObjectStore db(f.options);
  for (auto* sub : {&created, &idle}) {
    sub->operation = ObjectOperation::kErase;
    Q4T_CHECK(db.Execute(*sub, Access().deadline).ok());
    Q4T_CHECK(db.ReconcileWebhooks("", Access().deadline).ok());
    auto page = Events(db);
    Q4T_CHECK(page.ok() && page.value.GetArray("data")->array.size() ==
                               (sub == &created ? 1U : 2U));
  }
  auto final = Events(db);
  std::set<std::string> types;
  for (const auto& row : final.value.GetArray("data")->array) {
    const auto& notice = *row.Find("data");
    const auto& event = *notice.Find("event");
    types.insert(event.GetString("type"));
    Q4T_CHECK(event.Find("data")->GetString("id") == session.id);
    Q4T_CHECK(notice.GetArray("deliveries")->array.size() == 1);
  }
  Q4T_CHECK(types.contains("agent.session.idle") &&
            types.contains("agent.session.created"));
  return true;
}

Q4T_TEST(webhook_session_failure_and_retryable_turn_follow_public_state) {
  Fixture f;
  ObjectStore db(f.options);
  for (const auto type : {"agent.session.failed", "agent.session.idle"}) {
    auto id =
        type == std::string_view("agent.session.failed") ? "whe_f" : "whe_i";
    Q4T_CHECK(
        db.Execute(Subscription(Access().scope, id, type), Access().deadline)
            .ok());
  }
  for (const auto code : {"kernel_error", "service_draining"}) {
    auto run = Background(std::string("resp_") + code, "failed");
    const auto sid = std::string("agent_session_") + code;
    auto& response = Field(run.data, "response");
    Set(&response, "error",
        Object({{"code", String(code)},
                {"message", String("private error details")}}));
    Set(&response, "created_at", Number(100));
    Set(&response, "output", Array());
    Set(&run.data, "agent_session_id", String(sid));
    Set(&run.data, "agent_turn_finished_at", Number(102));
    Set(&run.data, "own_input", Array());
    ChatPlanConfig config;
    config.model = "local";
    AgentSessionStart start;
    auto create = Object({{"agent", Object({{"model", String("local")}})},
                          {"environment", Object({{"type", String("none")}})},
                          {"input", String("project lookup")}});
    Q4T_CHECK(PrepareDirectAgentSession(db, create, config, Access(), sid, 100,
                                        &start)
                  .ok());
    ObjectRequest session;
    session.kind = ObjectKind::kAgentSession;
    session.operation = ObjectOperation::kPut;
    session.scope = Access().scope;
    session.id = sid;
    session.data =
        Object({{"session", start.session},
                {"turns", Array({Object({{"id", String("turn_example")},
                                         {"response_id", String(run.id)}})})}});
    // Recovery can commit the response before the session mutation within
    // the same atomic batch. The projection must work in either row order.
    const std::array writes{run, session};
    Q4T_CHECK(db.ExecuteBatch(writes, Access().deadline).ok());
  }
  const auto page = Events(db);
  Q4T_CHECK(page.ok() && page.value.GetArray("data")->array.size() == 2);
  for (const auto& row : page.value.GetArray("data")->array) {
    const auto& event = *row.Find("data")->Find("event");
    const auto& payload = *event.Find("data");
    const bool failed = payload.GetString("id") == "agent_session_kernel_error";
    Q4T_CHECK(event.GetString("type") ==
              (failed ? "agent.session.failed" : "agent.session.idle"));
    Q4T_CHECK(payload.GetString("environment_type") == "none" &&
              payload.object.size() == 2);
    Q4T_CHECK(OutputJson(event).find("private error") == std::string::npos);
  }
  return true;
}

Q4T_TEST(webhook_warm_expiry_commits_on_idle_worker_and_skips_attached) {
  Fixture f;
  ObjectStore db(f.options);
  Q4T_CHECK(db.Execute(Subscription(Access().scope, "whe_expiry",
                                    "agent.environment.expired"),
                       Access().deadline)
                .ok());
  const auto due = std::time(nullptr) + 2;
  ObjectRequest env;
  env.kind = ObjectKind::kEnvironment;
  env.operation = ObjectOperation::kPut;
  env.id = "environment_unused";
  env.scope = Access().scope;
  env.ttl_seconds = 60;
  env.data =
      Object({{"resource", Object({{"id", String(env.id)},
                                   {"object", String("agent.environment")},
                                   {"type", String("openai_hosted")},
                                   {"status", String("ready")}})},
              {"warm_until", Number(due)}});
  auto created = db.Execute(env, Access().deadline);
  Q4T_CHECK(created.ok());
  auto bound = env;
  bound.id = "environment_bound";
  Set(&Field(bound.data, "resource"), "id", String(bound.id));
  Q4T_CHECK(db.Execute(bound, Access().deadline).ok());
  bound.expected_revision = 1;
  Set(&Field(bound.data, "resource"), "status", String("connected"));
  Q4T_CHECK(db.Execute(bound, Access().deadline).ok());
  // Inspect the fixture's own directory without sending a request that could
  // wake the object worker. The worker itself must commit the notice.
  const auto limit = std::chrono::steady_clock::now() + 5s;
  const auto notified = [&] {
    for (const auto& item :
         std::filesystem::directory_iterator(f.options.directory))
      if (item.path().filename().string().find(".webhook_event.") !=
          std::string::npos)
        return true;
    return false;
  };
  while (!notified() && std::chrono::steady_clock::now() < limit)
    std::this_thread::sleep_for(20ms);
  Q4T_CHECK(notified());
  env.operation = ObjectOperation::kGet;
  auto saved = db.Execute(env, Access().deadline);
  Q4T_CHECK(saved.ok() && saved.value.GetInt("expires_at") ==
                              created.value.GetInt("expires_at"));
  Q4T_CHECK(saved.value.Find("data")->Find("resource")->GetString("status") ==
            "expired");
  const auto page = Events(db);
  Q4T_CHECK(page.ok() && page.value.GetArray("data")->array.size() == 1);
  const auto& event =
      *page.value.GetArray("data")->array[0].Find("data")->Find("event");
  Q4T_CHECK(event.GetString("type") == "agent.environment.expired" &&
            event.GetInt("created_at") == due &&
            event.Find("data")->GetString("id") == env.id);
  bound.operation = ObjectOperation::kGet;
  Q4T_CHECK(db.Execute(bound, Access().deadline)
                .value.Find("data")
                ->Find("resource")
                ->GetString("status") == "connected");
  return true;
}

Q4T_TEST(environment_list_paginates_visible_resources_around_expired_entries) {
  Fixture f;
  ObjectStore db(f.options);
  const auto now = std::time(nullptr);
  for (int i = 0; i < 5; ++i) {
    ObjectRequest env;
    env.kind = ObjectKind::kEnvironment;
    env.operation = ObjectOperation::kPut;
    env.id = "environment_" + std::to_string(i);
    env.scope = Access().scope;
    env.created_at = now - 10 + i;
    const auto state = i == 0 || i == 2 || i == 4 ? "expired" : "connected";
    env.data = Object({{"resource", Object({{"id", String(env.id)},
        {"object", String("agent.environment")}, {"type", String("openai_hosted")},
        {"status", String(state)}, {"files", Array()}, {"skills", Array()},
        {"plugins", Array()}})}, {"warm_until", Number(now - 1)}});
    Q4T_CHECK(db.Execute(env, Access().deadline).ok());
  }
  auto list = [&](const std::string& query) {
    return HandleHostedEnvironment(db, "GET", "/v1/agents/environments" + query, "", Access(), "");
  };
  const auto one = list("?order=asc&limit=1");
  Q4T_CHECK(one.ok() && one.value.GetBool("has_more") &&
            one.value.GetString("first_id") == "environment_1");
  const auto two = list("?order=asc&limit=1&after=environment_1");
  Q4T_CHECK(two.ok() && !two.value.GetBool("has_more") &&
            two.value.GetString("last_id") == "environment_3");
  Q4T_CHECK(list("?type=self_hosted").http_status == 400);
  const auto empty = list("?after=environment_1&order=desc&limit=1");
  Q4T_CHECK(empty.ok() && empty.value.GetArray("data")->array.empty() &&
            !empty.value.GetBool("has_more"));
  return true;
}

}  // namespace
}  // namespace q4t::server
