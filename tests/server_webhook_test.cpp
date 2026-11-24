#include "../src/server/object_protocol.h"
#include "../src/server/webhook_keys.h"
#include "q4t/server/webhook.h"
#include "q4t/server/server_options.h"
#include "q4t/server/object_retention.h"
#include "q4t/test.h"

#include <array>
#include <chrono>
#include <cstdlib>
#include <filesystem>

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

}  // namespace
}  // namespace q4t::server
