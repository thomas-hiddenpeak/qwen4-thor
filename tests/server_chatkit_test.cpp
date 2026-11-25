#include "../src/server/chatkit_structured.h"
#include "../src/server/chatkit_credentials.h"
#include "../src/server/chatkit_retry.h"
#include "../src/server/chatkit_attachments.h"
#include "q4t/server/files.h"
#include "q4t/server/files_http.h"
#include "../src/server/chatkit_thread_state.h"
#include "../src/server/object_protocol.h"
#include "q4t/server/object_retention.h"
#include "q4t/server/http_connection.h"
#include "q4t/server/agent_registration.h"
#include "q4t/server/realtime_client_ticket.h"
#include "q4t/test.h"

#include <sys/socket.h>
#include <unistd.h>

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
ObjectReply Create(ObjectStore& objects, const StateAccess& access, int limit,
                   bool upload = false) {
  ChatPlanConfig config;
  config.model = "model-cpu";
  auto agent =
      HandleAgentRegistration(objects, "POST", "/v1/agents",
                              R"({"model":"model-cpu"})", config, access);
  if (!agent.ok()) return agent;
  auto wire = Object(
      {{"workflow", Object({{"id", String(agent.value.GetString("id"))}})},
       {"user", String("reader")},
       {"rate_limits",
        Object({{"max_requests_per_1_minute", Number(limit)}})}});
  if (upload)
    Set(&wire, "chatkit_configuration",
        Object({{"file_upload", Object({{"enabled", Boolean(true)},
                                        {"max_files", Number(1)},
                                        {"max_file_size", Number(1)}})}}));
  return HandleChatKit(objects, access, "POST", "/v1/chatkit/sessions",
                       OutputJson(wire), {kBinding, kIssuer});
}

Q4T_TEST(chatkit_forwarded_ticket_binds_current_gateway_key_owner_and_expiry) {
  const std::string opaque = "cksec_" + std::string(64, 'e');
  std::string token;
  Q4T_CHECK(SignRealtimeTicket(kBinding, kIssuer, "alpha", opaque, 120, &token,
                               ClientTicketAudience::kChatKit));
  const auto auth = "Q4T-ChatKit " + kIssuer + " Bearer " + token;
  std::string bearer;
  Q4T_CHECK(ResolveForwardedChatKit(auth, "alpha", kBinding, 119, &bearer));
  Q4T_CHECK(bearer == "Bearer " + token);
  Q4T_CHECK(!ResolveForwardedChatKit(auth, "beta", kBinding, 119, &bearer));
  Q4T_CHECK(!ResolveForwardedChatKit(auth, "alpha", kBinding, 120, &bearer));
  const auto rotated = "Q4T-ChatKit " + std::string(64, 'd') + " Bearer " + token;
  Q4T_CHECK(!ResolveForwardedChatKit(rotated, "alpha", kBinding, 119, &bearer));
  Q4T_CHECK(!ResolveForwardedChatKit(auth, "alpha", std::string(64, 'f'), 119, &bearer));
  return true;
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
Q4T_TEST(chatkit_retry_selects_legacy_user_identity_and_discards_only_future) {
  const auto part = [](const char* text) {
    return Array(
        {Object({{"type", String("input_text")}, {"text", String(text)}})});
  };
  const auto user = [&](const char* id, const char* text) {
    return Object({{"id", String(id)},
                   {"type", String("chatkit.user_message")},
                   {"content", part(text)},
                   {"attachments", Array()},
                   {"inference_options", {}}});
  };
  auto first = user("item_visible_a", "first");
  auto second = user("item_visible_b", "second");
  const auto assistant =
      Object({{"id", String("msg_old")},
              {"type", String("chatkit.assistant_message")}});
  auto data = Object({{"thread", Object({{"id", String("cthr_one")}})},
                      {"last_response", String("resp_old")},
                      {"items", Array({first, assistant, second})},
                      {"feedback", Object({{"msg_old", String("positive")}})}});
  const auto canonical = [&](const char* id, const char* text) {
    return Object({{"id", String(id)},
                   {"type", String("message")},
                   {"role", String("user")},
                   {"content", part(text)}});
  };
  const auto original = canonical("item_canonical_a", "first");
  const auto later = canonical("item_canonical_b", "second");
  const auto prior_answer = Object({{"id", String("msg_old")},
                                    {"type", String("message")},
                                    {"role", String("assistant")}});
  auto saved = Object({{"chatkit_thread_id", String("cthr_one")},
                       {"response", Object({{"id", String("resp_old")}})},
                       {"history", Array({original, prior_answer, later})}});
  const auto before = OutputJson(data);
  chatkit_detail::RetryHistory prefix;
  Json selected;
  Q4T_CHECK(chatkit_detail::SelectRetryHistory(saved, "missing", &data,
                                               &selected, &prefix)
                .ok());
  Q4T_CHECK(prefix.items.IsNull() && OutputJson(data) == before);
  const auto wrong = chatkit_detail::SelectRetryHistory(saved, "msg_old", &data,
                                                        &selected, &prefix);
  Q4T_CHECK(wrong.http_status == 400 &&
            wrong.value.GetString("param") == "item_id");
  auto foreign = saved;
  Set(&foreign, "chatkit_thread_id", String("cthr_two"));
  Q4T_CHECK(!chatkit_detail::SelectRetryHistory(foreign, "item_visible_a",
                                                &data, &selected, &prefix)
                 .ok());
  auto switched = saved;
  std::swap(Field(switched, "history").array.front(),
            Field(switched, "history").array.back());
  Q4T_CHECK(!chatkit_detail::SelectRetryHistory(switched, "item_visible_a",
                                                &data, &selected, &prefix)
                 .ok());
  Q4T_CHECK(OutputJson(data) == before);
  auto middle = data;
  chatkit_detail::RetryHistory retained;
  Q4T_CHECK(chatkit_detail::SelectRetryHistory(saved, "item_visible_b", &middle,
                                               &selected, &retained)
                .ok());
  ResponseStatePlan continuation;
  continuation.own_input = continuation.history = Array({later});
  Q4T_CHECK(chatkit_detail::ApplyRetryHistory(retained, &continuation).ok());
  Q4T_CHECK(continuation.own_input.array.size() == 1);
  Q4T_CHECK(OutputJson(continuation.history) ==
            OutputJson(*saved.Find("history")));
  Q4T_CHECK(middle.Find("feedback")->GetString("msg_old") == "positive");
  Q4T_CHECK(chatkit_detail::SelectRetryHistory(saved, "item_visible_a", &data,
                                               &selected, &prefix)
                .ok());
  Q4T_CHECK(selected.GetString("id") == "item_visible_a");
  Q4T_CHECK(data.GetArray("items")->array.size() == 1);
  Q4T_CHECK(data.Find("feedback")->object.empty() &&
            prefix.items.array.size() == 1);
  ResponseStatePlan plan;
  plan.history = Array({later});
  Q4T_CHECK(!chatkit_detail::ApplyRetryHistory(prefix, &plan).ok());
  plan.own_input = plan.history = Array({original});
  Q4T_CHECK(chatkit_detail::ApplyRetryHistory(prefix, &plan).ok());
  Q4T_CHECK(plan.history.array.size() == 1 &&
            plan.history.array.front().GetString("id") == "item_canonical_a");
  return true;
}

Q4T_TEST(chatkit_browser_page_defaults_and_management_zero_are_distinct) {
  Fixture fixture;
  ObjectStore objects(fixture.options);
  const auto access = Access();
  const auto session = Create(objects, access, 100);
  chatkit_detail::Grant grant;
  Q4T_CHECK(session.ok() &&
            chatkit_detail::Redeem(objects, access,
                                   session.value.GetString("client_secret"),
                                   std::time(nullptr), &grant)
                .ok());
  const auto workflow =
      grant.data.Find("session")->Find("workflow")->GetString("id");
  const auto start = std::time(nullptr) - 21;
  auto items = Array();
  const std::string last = "cthr_test_20";
  for (int i = 0; i < 21; ++i) {
    const auto item = "msg_test_" + std::to_string(i);
    items.array.push_back(Object({{"id", String(item)},
                                  {"thread_id", String(last)},
                                  {"object", String("chatkit.thread_item")},
                                  {"created_at", Number(start + i)},
                                  {"type", String("chatkit.assistant_message")},
                                  {"content", Array()}}));
    ObjectRequest write;
    write.kind = ObjectKind::kChatKitThread;
    write.operation = ObjectOperation::kPut;
    write.scope = access.scope;
    write.id = "cthr_test_" + std::to_string(i);
    write.created_at = start + i;
    const auto thread =
        Object({{"id", String(write.id)},
                {"user", String("reader")},
                {"object", String("chatkit.thread")},
                {"created_at", Number(write.created_at)},
                {"status", Object({{"type", String("active")}})},
                {"title", {}}});
    write.data = Object({{"thread", thread},
                         {"items", i == 20 ? items : Array()},
                         {"workflow", String(workflow)},
                         {"created_by", String(grant.session)}});
    Q4T_CHECK(objects.Execute(write, access.deadline).ok());
  }
  for (bool threads : {true, false}) {
    auto base = threads ? Object() : Object({{"thread_id", String(last)}});
    const char* op = threads ? "threads.list" : "items.list";
    auto list = [&](const Json& params) {
      return chatkit_detail::BrowserOperation(objects, access, grant, op,
                                              params);
    };
    const auto omitted = list(base);
    Q4T_CHECK(omitted.ok() && omitted.value.GetBool("has_more"));
    Q4T_CHECK(omitted.value.GetArray("data")->array.size() == 20);
    const auto prefix = std::string(threads ? "cthr_test_" : "msg_test_");
    Q4T_CHECK(omitted.value.GetString("after") == prefix + "1");
    for (const auto& limit : {Json{}, Number(0), Number(20)}) {
      auto query = base;
      Set(&query, "limit", limit);
      const auto result = list(query);
      Q4T_CHECK(result.ok() &&
                OutputJson(result.value) == OutputJson(omitted.value));
    }
    auto next = base;
    Set(&next, "limit", Number(0));
    Set(&next, "after", String(omitted.value.GetString("after")));
    const auto page = list(next);
    Q4T_CHECK(page.ok() && !page.value.GetBool("has_more") &&
              page.value.GetArray("data")->array.front().GetString("id") ==
                  prefix + "0");
    auto invalid = base;
    Set(&invalid, "order", {});
    const auto null_order = list(invalid);
    Q4T_CHECK(null_order.http_status == 400 &&
              null_order.value.GetString("param") == "order");
    // Malformed pagination is rejected before an item-list storage lookup.
    Json malformed;
    Q4T_CHECK(io::ParseJson(R"({"thread_id":"cthr_missing","limit":1.5})",
                            &malformed, true));
    const auto invalid_limit = list(malformed);
    Q4T_CHECK(invalid_limit.http_status == 400 &&
              invalid_limit.value.GetString("param") == "limit");
    if (!threads) {
      const auto missing_id = list(Object());
      Q4T_CHECK(missing_id.http_status == 400 &&
                missing_id.value.GetString("param") == "thread_id");
    }
    const auto route = threads ? "/v1/chatkit/threads"
                               : "/v1/chatkit/threads/" + last + "/items";
    const auto empty =
        HandleChatKit(objects, access, "GET", route + "?limit=0", "");
    Q4T_CHECK(empty.ok() && empty.value.GetArray("data")->array.empty() &&
              empty.value.GetBool("has_more"));
    const auto missing =
        HandleChatKit(objects, access, "GET", route + "?before=unknown", "");
    Q4T_CHECK(missing.http_status == 400 &&
              missing.value.GetString("param") == "before");
  }
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

Q4T_TEST(chatkit_feedback_is_atomic_tenant_scoped_and_durable) {
  Fixture fixture;
  const auto access = Access();
  const std::string id = "cthr_feedback";
  chatkit_detail::Grant grant;
  auto vote = [&](ObjectStore& store, const StateAccess& identity,
                  const chatkit_detail::Grant& user, Json targets,
                  const std::string& kind) {
    return chatkit_detail::BrowserOperation(
        store, identity, user, "items.feedback",
        Object({{"thread_id", String(id)},
                {"item_ids", std::move(targets)},
                {"kind", String(kind)}}));
  };
  ObjectRequest read;
  read.scope = access.scope;
  read.kind = ObjectKind::kChatKitThread;
  read.id = id;
  const auto ids = Array({String("msg_a"), String("msg_b")});
  {
    ObjectStore objects(fixture.options);
    auto missing_thread =
        Object({{"item_ids", ids}, {"kind", String("positive")}});
    auto invalid_thread = [&] {
      return chatkit_detail::BrowserOperation(objects, access, grant,
                                              "items.feedback", missing_thread);
    };
    Q4T_CHECK(invalid_thread().http_status == 400);
    Set(&missing_thread, "thread_id", Number(1));
    Q4T_CHECK(invalid_thread().http_status == 400);
    auto session = Create(objects, access, 32);
    Q4T_CHECK(session.ok() &&
              chatkit_detail::Redeem(objects, access,
                                     session.value.GetString("client_secret"),
                                     std::time(nullptr), &grant)
                  .ok());
    auto write = read;
    write.operation = ObjectOperation::kPut;
    auto thread = Object({{"id", String(id)},
                          {"object", String("chatkit.thread")},
                          {"created_at", Number(std::time(nullptr))},
                          {"title", {}},
                          {"status", Object({{"type", String("active")}})},
                          {"user", String("reader")}});
    auto items = Array();
    for (const auto& target : ids.array) {
      items.array.push_back(
          Object({{"id", target},
                  {"thread_id", String(id)},
                  {"type", String("chatkit.assistant_message")},
                  {"content", Array()}}));
    }
    write.data =
        Object({{"thread", thread},
                {"items", items},
                {"created_by", String(grant.session)},
                {"workflow",
                 *grant.data.Find("session")->Find("workflow")->Find("id")}});
    Q4T_CHECK(objects.Execute(write, access.deadline).ok());
    const auto initial = vote(objects, access, grant, ids, "positive");
    Q4T_CHECK(initial.ok() && OutputJson(initial.value) == "{}");
    const auto before = objects.Execute(read, access.deadline);
    Q4T_CHECK(before.ok());
    Q4T_CHECK(vote(objects, access, grant, ids, "positive").ok());
    Q4T_CHECK(vote(objects, access, grant, ids, "bad").http_status == 400);
    Q4T_CHECK(vote(objects, access, grant, Array({Number(1)}), "negative")
                  .http_status == 400);
    Q4T_CHECK(vote(objects, access, grant,
                   Array({String("msg_a"), String("msg_missing")}), "negative")
                  .http_status == 404);
    auto wrong = grant;
    Set(&Field(wrong.data, "session"), "user", String("second"));
    Q4T_CHECK(vote(objects, access, wrong, ids, "negative").http_status == 404);
    Q4T_CHECK(vote(objects, Access("client:beta"), grant, ids, "negative")
                  .http_status == 404);
    Q4T_CHECK(objects.Execute(read, access.deadline).value.GetInt("revision") ==
              before.value.GetInt("revision"));
    Q4T_CHECK(vote(objects, access, grant,
                   Array({String("msg_a"), String("msg_a")}), "negative")
                  .ok());
  }
  ObjectStore reopened(fixture.options);
  auto row = reopened.Execute(read, access.deadline);
  Q4T_CHECK(row.ok());
  const auto& data = Field(row.value, "data");
  auto* feedback = data.Find("feedback");
  Q4T_CHECK(feedback && feedback->object.size() == 2 &&
            feedback->GetString("msg_a") == "negative" &&
            feedback->GetString("msg_b") == "positive");
  const auto history = chatkit_detail::ClientThread(data);
  Q4T_CHECK(!history.Has("feedback") &&
            OutputJson(history).find("negative") == std::string::npos);
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
Q4T_TEST(chatkit_entity_mentions_preserve_browser_sdk_and_model_contracts) {
  auto text =
      Object({{"type", String("input_text")}, {"text", String("see ")}});
  auto tag = Object({{"type", String("input_tag")},
                     {"id", String("release")},
                     {"text", String("版本")},
                     {"data", Object({{"n", Number(7)}})}});
  Json content = Array(), model = Array();
  Q4T_CHECK(chatkit_detail::AppendUserContent(Array({text, tag, text}),
                                              &content, &model)
                .ok());
  Q4T_CHECK(model.array.size() == 1 &&
            model.array[0].GetString("text") == "see @版本see ");
  auto& rich = content.array.at(1);
  Q4T_CHECK(rich.GetString("id") == "release" &&
            rich.Find("data")->GetInt("n") == 7 &&
            rich.Find("group")->IsNull() && !rich.GetBool("interactive"));
  auto item = Object({{"type", String("chatkit.user_message")},
                      {"content", content},
                      {"created_at", Number(100)},
                      {"inference_options", {}}});
  Q4T_CHECK(chatkit_detail::ClientItem(item)
                .GetArray("content")
                ->array.at(1)
                .GetString("type") == "input_tag");
  chatkit_detail::ProjectManagementItem(&item);
  const auto& wire = item.GetArray("content")->array.at(1);
  Q4T_CHECK(wire.GetString("type") == "input_text" &&
            wire.GetString("text") == "@版本" && !wire.Has("data"));
  for (const auto& key : {"text", "id", "data", "group", "interactive"}) {
    auto wrong = tag;
    Set(&wrong, key, Number(3));
    Q4T_CHECK(
        chatkit_detail::AppendUserContent(Array({wrong}), &content, &model)
            .http_status == 400);
  }
  Set(&tag, "interactive", Boolean(true));
  Set(&tag, "group", String("docs"));
  Q4T_CHECK(
      chatkit_detail::AppendUserContent(Array({tag}), &content, &model).ok());
  Q4T_CHECK(content.array.back().GetBool("interactive") &&
            content.array.back().GetString("group") == "docs");
  model = Array();
  Q4T_CHECK(
      chatkit_detail::AppendUserContent(Array({text, text}), &content, &model)
          .ok());
  Q4T_CHECK(model.array.size() == 2 &&
            model.array[1].GetString("text") == "see ");
  return true;
}
Q4T_TEST(chatkit_structured_answers_keep_browser_and_management_identity) {
  Json args;
  Q4T_CHECK(io::ParseJson(
      R"({"inputs":[{"id":"pick","type":"multiple_choice","question":"Record?","options":[{"value":"release"}]},{"id":"notes","type":"freeform","question":"Details?"}]})",
      &args, true));
  auto call = Object({{"type", String("function_call")},
                      {"name", String("ask")},
                      {"id", String("fc_form")},
                      {"call_id", String("call_form")},
                      {"arguments", String(OutputJson(args))}});
  const auto response =
      Object({{"created_at", Number(17)}, {"output", Array({call})}});
  const auto plain = chatkit_detail::ProjectOutput(response, "cthr_project");
  Q4T_CHECK(plain.ok() && !plain.value.array.at(0).Has("browser_form"));
  auto rows = chatkit_detail::ProjectOutput(response, "cthr_project", "ask");
  Q4T_CHECK(rows.ok() && rows.value.array.size() == 1);
  auto& item = rows.value.array.front();
  const auto pending = chatkit_detail::ClientItem(item);
  Q4T_CHECK(pending.GetString("type") == "structured_input" &&
            pending.GetString("status") == "pending" &&
            pending.GetString("id") == "fc_form" && !pending.Has("call_id"));
  auto& form = Field(item, "browser_form");
  const auto before = OutputJson(form);
  Json invalid;
  Q4T_CHECK(io::ParseJson(R"({"answers":{"unknown":{"values":[7]}}})", &invalid,
                          true));
  Q4T_CHECK(chatkit_detail::SubmitStructuredInput(invalid, &form).http_status ==
                400 &&
            OutputJson(form) == before);
  Json input;
  Q4T_CHECK(io::ParseJson(
      R"({"answers":{"pick":{"values":["unlisted","ignore"]},"notes":{"values":["rev","506"]}}})",
      &input, true));
  Q4T_CHECK(chatkit_detail::SubmitStructuredInput(input, &form).ok());
  const auto answered = chatkit_detail::ClientItem(item);
  const auto& inputs = answered.GetArray("inputs")->array;
  Q4T_CHECK(inputs.at(0).Find("answer")->GetArray("values")->array.size() ==
                1 &&
            inputs.at(0).Find("answer")->GetArray("values")->array.at(0).str ==
                "unlisted" &&
            inputs.at(1).Find("answer")->GetArray("values")->array.size() == 2);
  Q4T_CHECK(chatkit_detail::SubmitStructuredInput(input, &form).http_status ==
            409);
  Set(&item, "output", String(OutputJson(form)));
  Set(&item, "status", String("completed"));
  chatkit_detail::ProjectManagementItem(&item);
  Q4T_CHECK(!item.Has("browser_form") &&
            item.GetString("type") == "chatkit.client_tool_call" &&
            item.GetString("id") == "fc_form" &&
            item.GetString("call_id") == "call_form" &&
            item.GetString("status") == "completed");
  Set(&call, "arguments", String(R"({"inputs":[]})"));
  const auto invalid_form = chatkit_detail::ProjectOutput(
      Object({{"output", Array({call})}}), "cthr_project", "ask");
  Q4T_CHECK(invalid_form.ok() &&
            !invalid_form.value.array.at(0).Has("browser_form"));
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
Q4T_TEST(
    chatkit_attachment_publication_requires_a_live_owner_and_actual_binary) {
  Fixture f;
  ObjectStore objects(f.options);
  const auto access = Access();
  const BinaryAccess binary_access{access.scope, access.deadline,
                                   access.cancelled};
  auto create_grant = [&](bool upload, chatkit_detail::Grant* grant) {
    auto created = Create(objects, access, 16, upload);
    if (!created.ok()) return created;
    return chatkit_detail::Redeem(objects, access,
                                  created.value.GetString("client_secret"),
                                  std::time(nullptr), grant);
  };
  chatkit_detail::Grant grant, other, disabled;
  Q4T_CHECK(create_grant(true, &grant).ok() &&
            create_grant(true, &other).ok() &&
            create_grant(false, &disabled).ok());
  uint64_t limit = 0;
  Q4T_CHECK(chatkit_detail::AttachmentLimit(disabled, &limit).http_status ==
            403);
  Q4T_CHECK(chatkit_detail::AttachmentLimit(grant, &limit).ok() &&
            limit == (1U << 20));
  BinaryStore binary(objects);
  BinaryStage stage;
  Q4T_CHECK(binary.Begin(binary_access, limit, 60, &stage).ok());
  Q4T_CHECK(binary.Append(binary_access, &stage, "project revision 476").ok());
  auto first = chatkit_detail::PublishAttachment(
      objects, binary_access, grant, &stage, "release.txt", "text/plain");
  Q4T_CHECK(first.ok() && stage.closed);
  const auto id = first.value.GetString("id");
  Json public_files = Array(), model = Array();
  auto resolve = [&](const chatkit_detail::Grant& owner,
                     const std::string& identity) {
    return chatkit_detail::ResolveAttachments(objects, access, owner,
                                              Array({String(identity)}),
                                              &public_files, &model);
  };
  Q4T_CHECK(resolve(other, id).http_status == 404 &&
            public_files.array.empty() && model.array.empty());
  Q4T_CHECK(resolve(grant, id).ok() &&
            model.array.back().GetString("file_id") == id &&
            public_files.array.back().GetString("mime_type") == "text/plain");
  BinaryRead read;
  Q4T_CHECK(Files(objects).Open(binary_access, id, &read).ok());
  std::string bytes;
  Q4T_CHECK(binary.Read(binary_access, read, 0, &bytes).ok() &&
            bytes == "project revision 476");
  Q4T_CHECK(binary.Close(binary_access, &read).ok());
  BinaryStage second;
  Q4T_CHECK(binary.Begin(binary_access, limit, 60, &second).ok());
  Q4T_CHECK(binary.Append(binary_access, &second, "second file").ok());
  auto publish = [&] {
    return chatkit_detail::PublishAttachment(objects, binary_access, grant,
                                             &second, "next.txt", "text/plain");
  };
  Q4T_CHECK(publish().code == "chatkit_attachment_limit" && !second.closed);
  Q4T_CHECK(chatkit_detail::RemoveAttachment(objects, access, other, id)
                .http_status == 404);
  Q4T_CHECK(chatkit_detail::RemoveAttachment(objects, access, grant, id).ok());
  Q4T_CHECK(resolve(grant, id).http_status == 404);
  Q4T_CHECK(publish().ok() && second.closed);
  BinaryStage revoked;
  Q4T_CHECK(binary.Begin(binary_access, limit, 60, &revoked).ok());
  Q4T_CHECK(binary.Append(binary_access, &revoked, "unfinished").ok());
  Q4T_CHECK(HandleChatKit(objects, access, "POST",
                          "/v1/chatkit/sessions/" + grant.session + "/cancel",
                          "")
                .ok());
  Q4T_CHECK(chatkit_detail::PublishAttachment(objects, binary_access, grant,
                                              &revoked, "lost.txt",
                                              "text/plain")
                .http_status == 401);
  Q4T_CHECK(binary.Abort(binary_access, &revoked).ok());
  return true;
}
Q4T_TEST(chatkit_upload_enforces_session_bytes_before_publication) {
  Fixture fixture;
  ObjectStore objects(fixture.options);
  const auto access = Access();
  auto session = Create(objects, access, 8, true);
  Q4T_CHECK(session.ok());
  const auto secret = session.value.GetString("client_secret");
  BinaryAccess binary{access.scope, access.deadline, access.cancelled};
  constexpr std::string_view type = "multipart/form-data; boundary=f";
  const auto limit = 1U << 20;
  {
    FileHttpRequest request(objects, binary);
    HttpBodyStream stream;
    Q4T_CHECK(request.PrepareChatKit("missing", type, &stream).http_status ==
                  401 &&
              !stream.consume);
    Q4T_CHECK(request.PrepareChatKit(secret, "application/json", &stream)
                      .http_status == 415 &&
              !stream.consume);
    Q4T_CHECK(request.PrepareChatKit(secret, type, &stream).ok() &&
              stream.max_bytes == limit + (kFileWireBytes - kFileBytes));
    const std::string header =
        "--f\r\nContent-Disposition: form-data; name=\"file\"; "
        "filename=\"readme.txt\"\r\nContent-Type: text/plain\r\n\r\n";
    Q4T_CHECK(stream.consume(header, access.deadline));
    Q4T_CHECK(!stream.consume(std::string(limit + 1, 'x') + "\r\n--f--\r\n",
                              access.deadline));
    Q4T_CHECK(stream.failure_status == 413 && request.UploadFailure() &&
              request.UploadFailure()->http_status == 413);
  }
  auto listed = Files(objects).List(binary, Object());
  Q4T_CHECK(listed.ok() && listed.value.GetArray("data")->array.empty());
  return true;
}

Q4T_TEST(chatkit_two_phase_document_binds_credential_bytes_and_publication) {
  Fixture fixture;
  ObjectStore objects(fixture.options);
  auto access = Access();
  const BinaryAccess binary{access.scope, access.deadline, access.cancelled};
  const auto session = Create(objects, access, 16, true);
  Q4T_CHECK(session.ok());
  const auto secret = session.value.GetString("client_secret");
  chatkit_detail::Grant grant;
  Q4T_CHECK(chatkit_detail::Redeem(objects, access, secret, std::time(nullptr),
                                   &grant)
                .ok());
  const std::string base = "https://example.test:8443/team";
  const std::string original("abc\0xyz", 7);
  auto reserve = [&](int64_t size) {
    return chatkit_detail::ReserveAttachment(
        objects, access, grant,
        Object({{"name", String("资料.bin")},
                {"size", Number(size)},
                {"mime_type", String("application/octet-stream")}}),
        {kBinding, kIssuer}, base);
  };
  Q4T_CHECK(reserve(-1).http_status == 400);
  Q4T_CHECK(reserve((1 << 20) + 1).http_status == 413);
  auto reservation = reserve(original.size());
  Q4T_CHECK(reservation.ok() && reserve(1).http_status == 409);
  const auto id = reservation.value.GetString("id");
  auto& desc = Field(reservation.value, "upload_descriptor");
  const auto url = desc.GetString("url");
  Q4T_CHECK(desc.GetString("method") == "PUT" && url.starts_with(base));
  const auto path = url.substr(base.size());
  const auto transfer = ParseChatKitTransfer("PUT", path);
  Q4T_CHECK(transfer.file == id && transfer.session == grant.session &&
            ParseChatKitTransfer("POST", path).file.empty() &&
            ParseChatKitTransfer("PUT", path + "?extra=1").file.empty());
  const auto& headers = Field(desc, "headers");
  const auto authorization = headers.GetString("Authorization");
  const auto token = BearerCredential(authorization);
  const auto mime = headers.GetString("Content-Type");
  std::string bearer;
  const auto envelope = "Q4T-ChatKit " + kIssuer + " " + authorization;
  Q4T_CHECK(token.starts_with("ckup_") && url.find(token) == url.npos &&
            ResolveForwardedChatKit(envelope, "alpha", kBinding,
                                    std::time(nullptr), &bearer,
                                    ClientTicketAudience::kChatKitUpload) &&
            !ResolveForwardedChatKit(envelope, "alpha", kBinding,
                                     std::time(nullptr), &bearer));
  ObjectRequest record;
  record.scope = access.scope;
  record.id = grant.session;
  record.kind = ObjectKind::kChatKitSession;
  auto saved = objects.Execute(record, access.deadline, access.cancelled);
  Q4T_CHECK(saved.ok() &&
            OutputJson(saved.value).find(token) == std::string::npos);
  BinaryRead read;
  Q4T_CHECK(Files(objects).Open(binary, id, &read).http_status == 404);
  Json files = Array(), model = Array();
  Q4T_CHECK(chatkit_detail::ResolveAttachments(
                objects, access, grant, Array({String(id)}), &files, &model)
                .http_status == 404);
  {
    FileHttpRequest request(objects, binary);
    HttpBodyStream stream;
    Q4T_CHECK(
        request.PrepareChatKit(secret, mime, &stream, transfer).http_status ==
        401);
    Q4T_CHECK(request.PrepareChatKit(token, "text/plain", &stream, transfer)
                  .http_status == 415);
  }
  {
    auto other = Access("client:beta");
    FileHttpRequest foreign(objects, {other.scope, other.deadline, {}});
    HttpBodyStream stream;
    Q4T_CHECK(
        foreign.PrepareChatKit(token, mime, &stream, transfer).http_status ==
        404);
  }
  // Both transfers open before either finishes. Only the first complete body
  // may publish; the other request must never replace its bytes.
  FileHttpRequest winner(objects, binary), duplicate(objects, binary);
  HttpBodyStream accepted, losing;
  Q4T_CHECK(winner.PrepareChatKit(token, mime, &accepted, transfer).ok() &&
            duplicate.PrepareChatKit(token, mime, &losing, transfer).ok() &&
            accepted.max_bytes == original.size());
  auto finish = [](FileHttpRequest& request) {
    int fd[2];
    if (socketpair(AF_UNIX, SOCK_STREAM | SOCK_CLOEXEC, 0, fd))
      return std::string{};
    request.Serve(fd[0]);
    close(fd[0]);
    char buffer[4096];
    auto n = recv(fd[1], buffer, sizeof(buffer), 0);
    close(fd[1]);
    return n > 0 ? std::string(buffer, n) : std::string{};
  };
  {
    FileHttpRequest short_upload(objects, binary);
    HttpBodyStream stream;
    Q4T_CHECK(short_upload.PrepareChatKit(token, mime, &stream, transfer).ok());
    Q4T_CHECK(stream.consume("abc", access.deadline));
    Q4T_CHECK(finish(short_upload).starts_with("HTTP/1.1 400"));
  }
  Q4T_CHECK(accepted.consume(original, access.deadline) &&
            losing.consume("changed", access.deadline));
  auto committed = finish(winner);
  Q4T_CHECK(committed.starts_with("HTTP/1.1 200") &&
            committed.find(id) != committed.npos &&
            committed.find("upload_descriptor") == committed.npos);
  Q4T_CHECK(finish(duplicate).starts_with("HTTP/1.1 401"));
  Q4T_CHECK(Files(objects).Open(binary, id, &read).ok());
  BinaryStore storage(objects);
  std::string bytes;
  Q4T_CHECK(storage.Read(binary, read, 0, &bytes).ok() && bytes == original);
  Q4T_CHECK(storage.Close(binary, &read).ok() &&
            chatkit_detail::RemoveAttachment(objects, access, grant, id).ok());
  auto pending = reserve(0);
  Q4T_CHECK(pending.ok());
  const auto empty_id = pending.value.GetString("id");
  const auto auth = Field(Field(pending.value, "upload_descriptor"), "headers")
                        .GetString("Authorization");
  FileHttpRequest empty(objects, binary);
  HttpBodyStream stream;
  Q4T_CHECK(empty
                .PrepareChatKit(BearerCredential(auth), mime, &stream,
                                {grant.session, empty_id})
                .ok() &&
            empty.WireLimit() == 0 && stream.max_bytes > 0);
  Q4T_CHECK(finish(empty).starts_with("HTTP/1.1 200"));
  return true;
}
Q4T_TEST(
    chatkit_two_phase_expiry_reclaims_capacity_and_revocation_stops_commit) {
  Fixture f;
  ObjectStore objects(f.options);
  const auto access = Access();
  const BinaryAccess binary{access.scope, access.deadline, {}};
  auto session = Create(objects, access, 12, true);
  Q4T_CHECK(session.ok());
  chatkit_detail::Grant grant;
  Q4T_CHECK(chatkit_detail::Redeem(objects, access,
                                   session.value.GetString("client_secret"),
                                   std::time(nullptr), &grant)
                .ok());
  auto reserve = [&] {
    return chatkit_detail::ReserveAttachment(
        objects, access, grant,
        Object({{"name", String("record.txt")},
                {"mime_type", String("text/plain")},
                {"size", Number(1)}}),
        {kBinding, kIssuer}, "http://localhost");
  };
  auto expired = reserve();
  Q4T_CHECK(expired.ok());
  const auto id = expired.value.GetString("id");
  const auto token = Field(Field(expired.value, "upload_descriptor"), "headers")
                         .GetString("Authorization");
  ObjectRequest row;
  row.scope = access.scope;
  row.kind = ObjectKind::kChatKitSession;
  row.id = grant.session;
  auto read = objects.Execute(row, access.deadline, {});
  Q4T_CHECK(read.ok());
  row.expected_revision = read.value.GetInt("revision");
  row.expires_at = read.value.GetInt("expires_at");
  row.data = Field(read.value, "data");
  auto& pending = Field(Field(row.data, "pending_attachments"), id);
  Set(&pending, "expires_at", Number(std::time(nullptr) - 1));
  row.operation = ObjectOperation::kPut;
  Q4T_CHECK(objects.Execute(row, access.deadline, {}).ok());
  chatkit_detail::ReservedFile file;
  Q4T_CHECK(chatkit_detail::ReadReservation(objects, access, &grant, id,
                                            BearerCredential(token), &file)
                .http_status == 401);
  auto current = reserve();
  Q4T_CHECK(current.ok() && current.value.GetString("id") != id);
  const auto new_id = current.value.GetString("id");
  const auto auth = Field(Field(current.value, "upload_descriptor"), "headers")
                        .GetString("Authorization");
  chatkit_detail::Grant redeemed;
  redeemed.session = grant.session;
  Q4T_CHECK(chatkit_detail::ReadReservation(objects, access, &redeemed, new_id,
                                            BearerCredential(auth), &file)
                .ok());
  Q4T_CHECK(file.name == "record.txt" && file.mime == "text/plain" &&
            file.bytes == 1);
  BinaryStore store(objects);
  BinaryStage stage;
  Q4T_CHECK(store.Begin(binary, 1, 60, &stage).ok() &&
            store.Append(binary, &stage, "x").ok());
  Q4T_CHECK(HandleChatKit(objects, access, "POST",
                          "/v1/chatkit/sessions/" + grant.session + "/cancel",
                          "")
                .ok());
  Q4T_CHECK(chatkit_detail::PublishAttachment(objects, binary, redeemed, &stage,
                                              file.name, file.mime, new_id,
                                              BearerCredential(auth))
                .http_status == 401);
  Q4T_CHECK(store.Abort(binary, &stage).ok());
  Q4T_CHECK(Files(objects)
                .List(binary, Object())
                .value.GetArray("data")
                ->array.empty());
  return true;
}
}  // namespace
}  // namespace q4t::server
