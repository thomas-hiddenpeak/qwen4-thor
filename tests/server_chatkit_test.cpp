#include "../src/server/chatkit_credentials.h"
#include "../src/server/object_protocol.h"
#include "q4t/server/agent_registration.h"
#include "q4t/server/realtime_client_ticket.h"
#include "q4t/test.h"

#include <cstdlib>
#include <ctime>
#include <filesystem>

namespace q4t::server {
namespace {
using namespace object_detail;
using namespace std::chrono_literals;
struct Fixture {
  ObjectStoreOptions options;
  std::filesystem::path directory;
  Fixture() {
    const char* output = std::getenv("Q4T_TEST_OUTPUT_DIR");
    auto base = std::filesystem::absolute(output ? output : ".q4t-work/chatkit");
    std::filesystem::create_directories(base);
    auto pattern = (base / "chatkit-XXXXXX").string();
    if (!mkdtemp(pattern.data())) throw std::runtime_error("ChatKit fixture");
    directory = pattern;
    options.directory = (directory / "objects").string();
  }
  ~Fixture() { std::filesystem::remove_all(directory); }
};
StateAccess Access(std::string scope = "client:alpha") {
  return {std::move(scope), std::chrono::steady_clock::now() + 10s, {}};
}
const std::string kBinding(64, 'a'), kIssuer(64, 'c');
ObjectReply Create(ObjectStore& objects, const StateAccess& access, int limit) {
  ChatPlanConfig config;
  config.model = "model-cpu";
  auto agent = HandleAgentRegistration(objects, "POST", "/v1/agents",
                                       R"({"model":"model-cpu"})", config, access);
  if (!agent.ok()) return agent;
  const auto wire = Object({
      {"workflow", Object({{"id", String(agent.value.GetString("id"))}})},
      {"user", String("reader")},
      {"rate_limits", Object({{"max_requests_per_1_minute", Number(limit)}})}});
  return HandleChatKit(objects, access, "POST", "/v1/chatkit/sessions",
                       OutputJson(wire), {kBinding, kIssuer});
}

Q4T_TEST(chatkit_session_survives_reopen_and_revokes_its_original_client) {
  Fixture fixture;
  auto access = Access();
  std::string id, token;
  {
    ObjectStore objects(fixture.options);
    auto created = Create(objects, access, 2);
    Q4T_CHECK(created.ok() && created.value.GetString("status") == "active");
    id = created.value.GetString("id");
    token = created.value.GetString("client_secret");
    RealtimeClientTicket ticket;
    Q4T_CHECK(!ReadRealtimeTicket(token, &ticket));
    Q4T_CHECK(ReadRealtimeTicket(token, &ticket, ClientTicketAudience::kChatKit));
    Q4T_CHECK(ticket.owner == "alpha" && VerifyRealtimeTicket(ticket, kBinding, kIssuer, std::time(nullptr)));
    Q4T_CHECK(!VerifyRealtimeTicket(ticket, kBinding, std::string(64, 'd'), std::time(nullptr)));
    ObjectRequest record;
    record.kind = ObjectKind::kChatKitSession;
    record.scope = access.scope;
    record.id = id;
    auto stored = objects.Execute(record, access.deadline);
    Q4T_CHECK(stored.ok() && OutputJson(stored.value).find(token) == std::string::npos);
  }
  ObjectStore objects(fixture.options);
  chatkit_detail::Grant grant;
  const auto now = std::time(nullptr);
  Q4T_CHECK(chatkit_detail::Redeem(objects, Access("client:beta"), token, now, &grant).http_status == 401);
  Q4T_CHECK(chatkit_detail::Redeem(objects, Access(), token, now, &grant).ok() && grant.session == id);
  Q4T_CHECK(chatkit_detail::Redeem(objects, Access(), token, now, &grant).ok());
  Q4T_CHECK(chatkit_detail::Redeem(objects, Access(), token, now, &grant).http_status == 429);
  Q4T_CHECK(chatkit_detail::Redeem(objects, Access(), token, now + 60, &grant).ok());
  Q4T_CHECK(chatkit_detail::Redeem(objects, Access(), token, now + 601, &grant).http_status == 401);
  const auto path = "/v1/chatkit/sessions/" + id + "/cancel";
  Q4T_CHECK(HandleChatKit(objects, Access("client:beta"), "POST", path, "").http_status == 404);
  for (int i = 0; i < 2; ++i) {
    auto result = HandleChatKit(objects, Access(), "POST", path, "");
    Q4T_CHECK(result.ok() && result.value.GetString("status") == "cancelled" &&
              result.value.GetString("client_secret") == token);
  }
  Q4T_CHECK(chatkit_detail::Redeem(objects, Access(), token, now, &grant).http_status == 401);
  return true;
}

Q4T_TEST(chatkit_session_body_and_read_routes_obey_sdk_and_http_bounds) {
  Fixture fixture;
  ObjectStore objects(fixture.options);
  for (const auto* body : {
           R"({"workflow":{"id":"x"},"user":""})",
           R"({"workflow":{"id":"x"},"user":"a","expires_after":{"anchor":"created_at","seconds":601}})",
           R"({"workflow":{"id":"x"},"user":"a","rate_limits":{"max_requests_per_1_minute":1.5}})"})
    Q4T_CHECK(HandleChatKit(objects, Access(), "POST", "/v1/chatkit/sessions", body).http_status == 400);
  Q4T_CHECK(HandleChatKit(objects, Access(), "POST", "/v1/chatkit/sessions",
      R"({"workflow":{"id":"agent_missing"},"user":"reader"})").http_status == 404);
  auto empty = HandleChatKit(objects, Access(), "GET", "/v1/chatkit/threads?limit=0", "");
  Q4T_CHECK(empty.ok() && empty.value.GetArray("data")->array.empty());
  Q4T_CHECK(HandleChatKit(objects, Access(), "GET", "/v1/chatkit/threads?limit=101", "").http_status == 400);
  Q4T_CHECK(HandleChatKit(objects, Access(), "DELETE", "/v1/chatkit/sessions", "").http_status == 405);
  Q4T_CHECK(!IsChatKitTarget("/v1/chatkit/sessions/cksess_x/cancel/extra"));
  return true;
}
}  // namespace
}  // namespace q4t::server
