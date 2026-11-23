#include "q4t/server/realtime_shape.h"

#include "q4t/server/realtime_client_secrets.h"
#include "q4t/server/realtime_client_ticket.h"
#include "../src/server/object_protocol.h"

#include <filesystem>
#include <fstream>
#include <iterator>
#include <atomic>
#include <thread>
#include <set>

#include "q4t/server/realtime_types_data.h"
#include "q4t/trace/sha256.h"
#include "q4t/test.h"

Q4T_TEST(realtime_shape_matches_actual_sdk_type_oracle) {
  using namespace q4t::server;
  const auto path = std::filesystem::path(__FILE__).parent_path() /
                    "fixtures/realtime/sdk_vectors.json";
  std::ifstream input(path);
  Q4T_CHECK(input.good());
  const std::string text{std::istreambuf_iterator<char>(input), {}};
  q4t::io::Json document;
  Q4T_CHECK(q4t::io::ParseJson(text, &document));
  Q4T_CHECK(document.GetString("profile") ==
            "q4t-sdk-realtime-shape-oracle-v1");
  Q4T_CHECK(document.GetString("validation_mode") ==
            "concrete-sdk-union-members");
  const auto data = realtime_types::kData;
  const auto hash = q4t::trace::HexDigest(q4t::trace::Sha256(
      {reinterpret_cast<const uint8_t*>(data.data()), data.size()}));
  Q4T_CHECK(document.GetString("graph_sha256") == hash);
  const auto* vectors = document.GetArray("vectors");
  Q4T_CHECK(vectors && vectors->array.size() >= 300);
  size_t differences = 0;
  for (const auto& vector : vectors->array) {
    const auto shape = vector.GetString("shape");
    const auto kind = shape == "conversation_item" ? RealtimeShape::kItem
                      : shape == "client_event"    ? RealtimeShape::kClientEvent
                      : shape == "server_event"    ? RealtimeShape::kServerEvent
                      : shape == "response_create"
                          ? RealtimeShape::kResponseCreate
                      : shape == "client_secret_create" ? RealtimeShape::kClientSecretCreate
                      : shape == "client_secret_created" ? RealtimeShape::kClientSecretCreated
                      : shape == "session" ? RealtimeShape::kSession
                      : shape == "call_accept" ? RealtimeShape::kCallAccept
                      : shape == "call_refer"  ? RealtimeShape::kCallRefer
                      : shape == "call_reject" ? RealtimeShape::kCallReject
                                               : RealtimeShape::kResponse;
    const auto* value = vector.Find("input");
    Q4T_CHECK(value);
    ChatPlanError error;
    const auto status = ValidateRealtimeShape(*value, kind, &error);
    if (status.ok() != vector.GetBool("accepted")) {
      ++differences;
      std::fprintf(stderr, "Realtime SDK difference: %s (%s)\n",
                   vector.GetString("name").c_str(), status.message().c_str());
    }
  }
  std::printf("Realtime SDK vectors: %zu, differences: %zu\n",
              vectors->array.size(), differences);
  Q4T_CHECK(differences == 0);
  return true;
}

Q4T_TEST(realtime_shape_sip_call_controls_preserve_their_own_contract) {
  using namespace q4t::server;
  const auto valid = [](RealtimeShape shape, const char* body) {
    q4t::io::Json value;
    ChatPlanError error;
    return q4t::io::ParseJson(body, &value, true) &&
           ValidateRealtimeWireShape(value, shape, &error, "body").ok();
  };
  const auto accept = RealtimeShape::kCallAccept;
  Q4T_CHECK(valid(accept, R"({"type":"realtime","instructions":"help"})"));
  Q4T_CHECK(valid(accept, R"({"type":"realtime","audio":{"input":{"transcription":null,"noise_reduction":null}}})"));
  Q4T_CHECK(!valid(accept, R"({"type":"realtime","id":"sess_forged"})"));
  Q4T_CHECK(!valid(accept, R"({"session":{"type":"live"}})"));
  Q4T_CHECK(!valid(accept, R"({"type":"transcription"})"));
  Q4T_CHECK(!valid(accept, R"({"type":"realtime","audio":{"input":{"transcription":null,"extra":true}}})"));
  const auto reject = RealtimeShape::kCallReject;
  for (const auto* body : {"null", "{}", R"({"status_code":300})",
                           R"({"status_code":699})"})
    Q4T_CHECK(valid(reject, body));
  for (const auto* body : {"[]", R"({"status_code":null})",
                           R"({"status_code":299})", R"({"status_code":700})",
                           R"({"status_code":486.5})", R"({"extra":true})"})
    Q4T_CHECK(!valid(reject, body));
  const auto refer = RealtimeShape::kCallRefer;
  Q4T_CHECK(valid(refer, R"({"target_uri":"tel:+12025550101"})"));
  Q4T_CHECK(!valid(refer, R"({"target_uri":"sips:user%0@desk"})"));
  Q4T_CHECK(!valid(refer, R"({"target_uri":"tel:+1","session":{}})"));
  return true;
}

Q4T_TEST(realtime_shape_duplicate_keys_and_bounded_error_path) {
  using namespace q4t::server;
  q4t::io::Json value;
  Q4T_CHECK(q4t::io::ParseJson(
      R"({"type":"function_call_output","call_id":"c","output":""})", &value));
  ChatPlanError error;
  Q4T_CHECK(ValidateRealtimeShape(value, RealtimeShape::kItem, &error));
  value.object.push_back(value.object.back());
  Q4T_CHECK(!ValidateRealtimeShape(value, RealtimeShape::kItem, &error));
  value.object.back().first = std::string(10000, 'x');
  Q4T_CHECK(!ValidateRealtimeShape(value, RealtimeShape::kItem, &error));
  Q4T_CHECK(error.param.size() < 128);
  Q4T_CHECK(!ValidateRealtimeShape(value, RealtimeShape::kItem, nullptr));
  return true;
}

namespace {
const std::string binding(64,'a'), issuer_key(64,'b');
const q4t::server::RealtimeSecretIssuer alpha{"alpha", binding, issuer_key};
const q4t::server::RealtimeSecretIssuer beta{"beta", binding, issuer_key};
}  // namespace

Q4T_TEST(realtime_client_secret_issue_redeem_and_expire_at_either_clock) {
  using namespace q4t::server;
  using namespace q4t::server::object_detail;
  using namespace std::chrono_literals;
  const auto resource = [](std::string type) {
    return Object({{"id", String("sess_example")},
                   {"object", String(type == "realtime"
                                         ? "realtime.session"
                                         : "realtime.transcription_session")},
                   {"type", String(type)}});
  };
  RealtimeClientSecrets secrets;
  const RealtimeSecretTime now{1000, {}};
  const auto first =
      secrets.Issue(alpha, Object(), resource("realtime"), now);
  Q4T_CHECK(first.ok() && first.value.GetInt("expires_at") == 1600);
  const std::string key = first.value.GetString("value");
  RealtimeSecretGrant grant;
  Q4T_CHECK(key.starts_with("ek_") && secrets.Resolve(key, &grant, now).ok());
  Q4T_CHECK(grant.owner == "alpha" &&
            grant.session.GetString("type") == "realtime");
  auto request =
      Object({{"expires_after", Object({{"seconds", Number(10)}})},
              {"session", Object({{"type", String("transcription")}})}});
  auto second = secrets.Issue(beta, request, resource("transcription"), now);
  Q4T_CHECK(second.ok() && second.value.GetInt("expires_at") == 1010);
  auto other = second.value.GetString("value");
  Q4T_CHECK(key != other);
  Q4T_CHECK(secrets.Resolve(key, &grant, {1599, now.monotonic + 599s}).ok());
  auto changed = key;
  changed.back() = changed.back() == '0' ? '1' : '0';
  for (const auto& invalid : {changed, key + "0", key.substr(1)})
    Q4T_CHECK(secrets.Resolve(invalid, &grant, now).http_status == 401);
  Q4T_CHECK(
      secrets.Resolve(key, &grant, {1000, now.monotonic + 600s}).http_status ==
      401);
  Q4T_CHECK(grant.owner == "alpha" &&
            grant.session.GetString("type") == "realtime");
  Q4T_CHECK(secrets.Resolve(other, &grant, {1009, now.monotonic + 1s}).ok());
  Q4T_CHECK(grant.owner == "beta" &&
            grant.session.GetString("type") == "transcription");
  Q4T_CHECK(
      secrets.Resolve(other, &grant, {1010, now.monotonic + 1s}).http_status ==
      401);
  Q4T_CHECK(secrets.Resolve(other, &grant, now).http_status == 401);
  return true;
}

Q4T_TEST(
    realtime_client_secret_rejects_invalid_ttls_and_private_resource_fields) {
  using namespace q4t::server;
  using namespace q4t::server::object_detail;
  RealtimeClientSecrets store;
  const RealtimeSecretTime now{100, {}};
  const auto resource = Object({{"type", String("realtime")},
                                {"id", String("sess_f")},
                                {"object", String("realtime.session")}});
  auto issue = [&](const char* raw) {
    q4t::io::Json request;
    if (!q4t::io::ParseJson(raw, &request)) throw std::logic_error("fixture");
    return store.Issue(alpha, request, resource, now);
  };
  for (
      const char* raw :
      {R"({"expires_after":{"seconds":9}})",
       R"({"expires_after":{"seconds":7201}})",
       R"({"expires_after":{"seconds":10.1}})",
       R"({"expires_after":{"seconds":"600"}})",
       R"({"expires_after":{"anchor":"used_at"}})",
       R"({"session":{"type":"realtime","id":"sess_injected"}})",
       R"({"session":{"type":"realtime","audio":{"input":{"transcription":null,"other":true}}}})"})
    Q4T_CHECK(issue(raw).http_status == 400);
  Q4T_CHECK(issue(R"({"expires_after":{"anchor":"created_at","seconds":7200}})")
                .value.GetInt("expires_at") == 7300);
  Q4T_CHECK(
      issue(
          R"({"session":{"type":"realtime","audio":{"input":{"transcription":null}}}})")
          .ok());
  auto unsafe = resource;
  Set(&unsafe, "tools",
      Array({Object({{"type", String("mcp")},
                     {"authorization", String("private_fixture")}})}));
  const auto failed = store.Issue(alpha, Object(), unsafe, now);
  Q4T_CHECK(!failed.ok() && !failed.value.Has("value"));
  return true;
}

Q4T_TEST(realtime_client_secret_capacity_is_scoped_and_expiry_releases_it) {
  using namespace q4t::server;
  using namespace q4t::server::object_detail;
  const auto resource = Object({{"type", String("realtime")},
                                {"id", String("sess_f")},
                                {"object", String("realtime.session")}});
  RealtimeClientSecrets store;
  std::mutex lock;
  std::set<std::string> issued;
  std::atomic<int> limited{0}, errors{0};
  {
    std::vector<std::jthread> workers;
    for (int i = 0; i < 8; ++i) {
      workers.emplace_back([&] {
        for (int j = 0; j < 10; ++j) {
          const auto reply =
              store.Issue(alpha, Object(), resource, {100, {}});
          if (reply.http_status == 429) {
            ++limited;
          } else if (reply.ok()) {
            std::lock_guard guard(lock);
            issued.insert(reply.value.GetString("value"));
          } else {
            ++errors;
          }
        }
      });
    }
  }
  Q4T_CHECK(issued.size() == 64 && limited == 16 && errors == 0);
  Q4T_CHECK(store.Issue(alpha, Object(), resource, {699, {}}).http_status ==
            429);
  Q4T_CHECK(store.Issue(beta, Object(), resource, {699, {}}).ok());
  Q4T_CHECK(store.Issue(alpha, Object(), resource, {700, {}}).ok());
  return true;
}
