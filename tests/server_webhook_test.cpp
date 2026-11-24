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
}  // namespace
}  // namespace q4t::server
