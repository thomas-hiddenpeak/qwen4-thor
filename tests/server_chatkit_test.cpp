#include "../src/server/chatkit_credentials.h"
#include "../src/server/chatkit_thread_state.h"
#include "../src/server/object_protocol.h"
#include "q4t/server/object_retention.h"
#include "q4t/server/http_connection.h"
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
    auto base =
        std::filesystem::absolute(output ? output : ".q4t-work/chatkit");
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
  auto agent =
      HandleAgentRegistration(objects, "POST", "/v1/agents",
                              R"({"model":"model-cpu"})", config, access);
  if (!agent.ok()) return agent;
  const auto wire = Object(
      {{"workflow", Object({{"id", String(agent.value.GetString("id"))}})},
       {"user", String("reader")},
       {"rate_limits",
        Object({{"max_requests_per_1_minute", Number(limit)}})}});
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
    Q4T_CHECK(
        ReadRealtimeTicket(token, &ticket, ClientTicketAudience::kChatKit));
    Q4T_CHECK(
        ticket.owner == "alpha" &&
        VerifyRealtimeTicket(ticket, kBinding, kIssuer, std::time(nullptr)));
    Q4T_CHECK(!VerifyRealtimeTicket(ticket, kBinding, std::string(64, 'd'),
                                    std::time(nullptr)));
    ObjectRequest record;
    record.kind = ObjectKind::kChatKitSession;
    record.scope = access.scope;
    record.id = id;
    auto stored = objects.Execute(record, access.deadline);
    Q4T_CHECK(stored.ok() &&
              OutputJson(stored.value).find(token) == std::string::npos);
  }
  ObjectStore objects(fixture.options);
  chatkit_detail::Grant grant;
  const auto now = std::time(nullptr);
  Q4T_CHECK(
      chatkit_detail::Redeem(objects, Access("client:beta"), token, now, &grant)
          .http_status == 401);
  Q4T_CHECK(
      chatkit_detail::Redeem(objects, Access(), token, now, &grant).ok() &&
      grant.session == id);
  Q4T_CHECK(chatkit_detail::Redeem(objects, Access(), token, now, &grant).ok());
  Q4T_CHECK(chatkit_detail::Redeem(objects, Access(), token, now, &grant)
                .http_status == 429);
  Q4T_CHECK(
      chatkit_detail::Redeem(objects, Access(), token, now + 60, &grant).ok());
  Q4T_CHECK(chatkit_detail::Redeem(objects, Access(), token, now + 601, &grant)
                .http_status == 401);
  const auto path = "/v1/chatkit/sessions/" + id + "/cancel";
  Q4T_CHECK(HandleChatKit(objects, Access("client:beta"), "POST", path, "")
                .http_status == 404);
  for (int i = 0; i < 2; ++i) {
    auto result = HandleChatKit(objects, Access(), "POST", path, "");
    Q4T_CHECK(result.ok() && result.value.GetString("status") == "cancelled" &&
              result.value.GetString("client_secret") == token);
  }
  Q4T_CHECK(chatkit_detail::Redeem(objects, Access(), token, now, &grant)
                .http_status == 401);
  return true;
}

Q4T_TEST(chatkit_session_body_and_read_routes_obey_sdk_and_http_bounds) {
  Fixture fixture;
  ObjectStore objects(fixture.options);
  for (
      const auto* body :
      {R"({"workflow":{"id":"x"},"user":""})",
       R"({"workflow":{"id":"x"},"user":"a","expires_after":{"anchor":"created_at","seconds":601}})",
       R"({"workflow":{"id":"x"},"user":"a","rate_limits":{"max_requests_per_1_minute":1.5}})"})
    Q4T_CHECK(
        HandleChatKit(objects, Access(), "POST", "/v1/chatkit/sessions", body)
            .http_status == 400);
  Q4T_CHECK(
      HandleChatKit(objects, Access(), "POST", "/v1/chatkit/sessions",
                    R"({"workflow":{"id":"agent_missing"},"user":"reader"})")
          .http_status == 404);
  auto empty = HandleChatKit(objects, Access(), "GET",
                             "/v1/chatkit/threads?limit=0", "");
  Q4T_CHECK(empty.ok() && empty.value.GetArray("data")->array.empty());
  Q4T_CHECK(HandleChatKit(objects, Access(), "GET",
                          "/v1/chatkit/threads?limit=101", "")
                .http_status == 400);
  Q4T_CHECK(
      HandleChatKit(objects, Access(), "DELETE", "/v1/chatkit/sessions", "")
          .http_status == 405);
  Q4T_CHECK(!IsChatKitTarget("/v1/chatkit/sessions/cksess_x/cancel/extra"));
  return true;
}
Q4T_TEST(
    chatkit_thread_history_enforces_identity_workflow_retention_and_cursors) {
  Fixture fixture;
  ObjectStore objects(fixture.options);
  const auto access = Access();
  const auto now = std::time(nullptr);
  const auto owner = Object(
      {{"user", String("reader")},
       {"workflow", Object({{"id", String("agent_a")}})},
       {"chatkit_configuration",
        Object({{"history", Object({{"enabled", Boolean(true)},
                                    {"recent_threads", Number(1)}})}})}});
  chatkit_detail::Grant grant{"cksess_current", Object({{"session", owner}})};
  auto submit = [&](std::string id, std::string user, std::string workflow,
                    bool own, bool private_history, int seconds) {
    ObjectRequest req;
    req.operation = ObjectOperation::kPut;
    req.scope = access.scope;
    req.kind = ObjectKind::kChatKitThread;
    req.id = id;
    req.created_at = now - seconds;
    auto thread = Object({{"id", String(id)},
                          {"user", String(user)},
                          {"object", String("chatkit.thread")},
                          {"created_at", Number(req.created_at)},
                          {"status", Object({{"type", String("active")}})},
                          {"title", {}}});
    req.data =
        Object({{"thread", std::move(thread)},
                {"items", Array()},
                {"workflow", String(workflow)},
                {"session_only", Boolean(private_history)},
                {"created_by", String(own ? grant.session : "cksess_other")}});
    return objects.Execute(req, access.deadline);
  };
  Q4T_CHECK(submit("cthr_earlier", "reader", "agent_a", false, false, 6).ok());
  Q4T_CHECK(submit("cthr_latest", "reader", "agent_a", false, false, 5).ok());
  Q4T_CHECK(submit("cthr_hidden", "reader", "agent_a", false, true, 4).ok());
  Q4T_CHECK(submit("cthr_person", "another", "agent_a", false, false, 3).ok());
  Q4T_CHECK(submit("cthr_workflow", "reader", "agent_b", false, false, 2).ok());
  Q4T_CHECK(submit("cthr_own", "reader", "agent_a", true, false, 1).ok());
  auto list = chatkit_detail::BrowserOperation(
      objects, access, grant, "threads.list", Object({{"limit", Number(1)}}));
  Q4T_CHECK(list.ok() && list.value.GetBool("has_more") &&
            list.value.GetString("after") == "cthr_own");
  auto next = chatkit_detail::BrowserOperation(
      objects, access, grant, "threads.list",
      Object({{"after", String("cthr_own")}, {"limit", Number(1)}}));
  Q4T_CHECK(next.ok() && !next.value.GetBool("has_more") &&
            next.value.GetArray("data")->array.front().GetString("id") ==
                "cthr_latest");
  for (const auto* id :
       {"cthr_person", "cthr_workflow", "cthr_earlier", "cthr_hidden"}) {
    for (const auto* op : {"threads.get_by_id", "items.list", "threads.update",
                           "threads.delete"}) {
      auto result = chatkit_detail::BrowserOperation(
          objects, access, grant, op,
          Object({{"thread_id", String(id)}, {"title", String("other")}}));
      Q4T_CHECK(result.http_status == 404);
    }
  }
  auto& history = Field(
      Field(Field(grant.data, "session"), "chatkit_configuration"), "history");
  Set(&history, "enabled", Boolean(false));
  auto read = [&](const char* id) {
    return chatkit_detail::ReadThread(objects, access, id, &grant);
  };
  Q4T_CHECK(read("cthr_own").ok() && read("cthr_latest").http_status == 404);
  auto renamed = chatkit_detail::BrowserOperation(
      objects, access, grant, "threads.update",
      Object(
          {{"thread_id", String("cthr_own")}, {"title", String("New title")}}));
  Q4T_CHECK(renamed.ok() && renamed.value.GetString("title") == "New title");
  auto erased = chatkit_detail::BrowserOperation(
      objects, access, grant, "threads.delete",
      Object({{"thread_id", String("cthr_own")}}));
  Q4T_CHECK(erased.ok() && read("cthr_own").http_status == 404);
  return true;
}
Q4T_TEST(
    chatkit_retains_only_live_dependencies_and_places_requests_in_correct_lanes) {
  const auto thread =
      Object({{"thread", Object({{"id", String("cthr_history")}})},
              {"items", Array()},
              {"pending_response", String("resp_active")},
              {"last_response", String("resp_prior")}});
  auto refs = InspectObjectRetention("chatkit_thread", thread);
  Q4T_CHECK(!refs.uncertain &&
            (refs.references ==
             std::vector<std::string>{"resp_active", "resp_prior"}));
  auto session = Object({{"session", Object({{"id", String("cksess_token")},
                                             {"status", String("active")},
                                             {"expires_at", Number(120)}})},
                         {"credential_ids", Array({String("cred_one")})},
                         {"workflow_snapshot", String("sealed")}});
  auto creds = InspectObjectRetention("chatkit_session", session);
  Q4T_CHECK(!creds.uncertain && creds.references_before == 120 &&
            creds.references == std::vector<std::string>{"cred_one"});
  Set(&Field(session, "session"), "status", String("cancelled"));
  Q4T_CHECK(
      InspectObjectRetention("chatkit_session", session).references.empty());
  Q4T_CHECK(InspectObjectRetention("chatkit_thread", Object()).uncertain);
  Q4T_CHECK(ClassifyHttpConnection("POST", "/v1/chatkit/sessions") ==
            HttpConnectionClass::kBusiness);
  Q4T_CHECK(
      ClassifyHttpConnection("POST", "/v1/chatkit/sessions/cksess_id/cancel") ==
      HttpConnectionClass::kControl);
  Q4T_CHECK(
      ClassifyHttpConnection("GET", "/v1/chatkit/threads/cthr_id/items") ==
      HttpConnectionClass::kObservation);
  Q4T_CHECK(ClassifyHttpConnection("POST", "/v1/chatkit") ==
            HttpConnectionClass::kBusiness);
  return true;
}

Q4T_TEST(chatkit_keeps_original_journal_until_thread_advances_or_is_deleted) {
  Fixture f;
  ObjectStore objects(f.options);
  const auto access = Access();
  ObjectRequest thread;
  thread.kind = ObjectKind::kChatKitThread;
  thread.operation = ObjectOperation::kPut;
  thread.scope = access.scope;
  thread.id = "cthr_used";
  thread.data = Object({{"thread", Object({{"id", String(thread.id)}})},
                        {"items", Array()},
                        {"last_response", String("resp_used")}});
  Q4T_CHECK(objects.Execute(thread, access.deadline).ok());
  ObjectRequest response;
  response.kind = ObjectKind::kResponse;
  response.operation = ObjectOperation::kPut;
  response.scope = access.scope;
  response.id = "resp_used";
  response.data = Object({{"response", Object({{"id", String(response.id)},
                                               {"status", String("completed")},
                                               {"output", Array()}})},
                          {"chatkit_thread_id", String(thread.id)}});
  Q4T_CHECK(objects.Execute(response, access.deadline).ok());
  ResponsesState state(objects);
  Q4T_CHECK(state.Erase(response.id, access).code ==
            "chatkit_thread_owns_response");
  thread.expected_revision = 1;
  Set(&thread.data, "last_response", String("resp_next"));
  Q4T_CHECK(objects.Execute(thread, access.deadline).ok());
  Q4T_CHECK(state.Erase(response.id, access).ok());
  return true;
}
Q4T_TEST(chatkit_tool_call_preserves_sdk_and_browser_shapes_and_user_quotes) {
  Json response;
  Q4T_CHECK(io::ParseJson(
      R"({"created_at":100,"output":[{"type":"function_call","id":"fc_x","call_id":"call_x","name":"lookup","arguments":"{}"}]})",
      &response, true));
  auto output = chatkit_detail::ProjectOutput(response, "cthr_1");
  Q4T_CHECK(output.ok() && output.value.array.size() == 1);
  auto item = output.value.array.front();
  Q4T_CHECK(item.GetString("arguments") == "{}" &&
            item.GetString("status") == "in_progress");
  auto browser = chatkit_detail::ClientItem(item);
  Q4T_CHECK(browser.GetString("status") == "pending" &&
            browser.Find("arguments")->IsObject());
  Set(&item, "status", String("completed"));
  Set(&item, "output", String(R"({"revision":476})"));
  browser = chatkit_detail::ClientItem(item);
  Q4T_CHECK(browser.Find("output")->GetInt("revision") == 476 &&
            browser.GetString("call_id") == "call_x");
  Set(&Field(response, "output").array.front(), "arguments", String("1"));
  Q4T_CHECK(!chatkit_detail::ProjectOutput(response, "cthr_1").ok());
  Json user;
  Q4T_CHECK(io::ParseJson(
      R"({"type":"chatkit.user_message","content":[{"type":"quoted_text","text":"context"},{"type":"input_text","text":"question"}],"inference_options":null,"created_at":100})",
      &user, true));
  auto projected = chatkit_detail::ClientItem(user);
  Q4T_CHECK(projected.GetString("quoted_text") == "context" &&
            projected.GetArray("content")->array.size() == 1);
  return true;
}
}  // namespace
}  // namespace q4t::server
