#include "../src/server/chatkit_actions.h"
#include "../src/server/chatkit_generation_stream.h"
#include "../src/server/chatkit_workflow.h"
#include "q4t/server/http_response.h"
#include "../src/server/chatkit_structured.h"
#include "../src/server/chatkit_credentials.h"
#include "../src/server/chatkit_retry.h"
#include "../src/server/chatkit_attachments.h"
#include "q4t/io/base64.h"
#include "q4t/server/file_image.h"
#include "q4t/server/lifetime.h"
#include "q4t/vision/processor.h"
#include "q4t/server/files.h"
#include "q4t/server/files_http.h"
#include "../src/server/chatkit_thread_state.h"
#include "../src/server/object_protocol.h"
#include "q4t/server/object_retention.h"
#include "q4t/server/http_connection.h"
#include "q4t/server/agent_registration.h"
#include "q4t/server/realtime_client_ticket.h"
#include "q4t/server/responses_item_shape.h"
#include "q4t/test.h"

#include <sys/socket.h>
#include <unistd.h>

#include <cstdlib>
#include <ctime>
#include <filesystem>
#include <fstream>

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
Q4T_TEST(chatkit_actions_bind_owner_and_commit_state_with_widget) {
  Fixture fixture;
  ObjectStore objects(fixture.options);
  const auto access = Access();
  const auto now = std::time(nullptr);
  const auto workflow = String("agent_app");
  const auto session =
      Object({{"user", String("reader")},
              {"workflow", Object({{"id", workflow}})},
              {"chatkit_configuration",
               Object({{"history", Object({{"enabled", Boolean(true)}})}})}});
  chatkit_detail::Grant grant{"cksess_local", Object({{"session", session}})};
  const auto handlers = Object(
      {{"show", String("text({state:actionContext.action.payload,widget:{type:'"
                       "Card',children:[]}})")},
       {"finish",
        String("progress('checking');text({notices:[{level:'info',"
               "message:'saved'}],title:'Reviewed',remove_sender:true})")},
       {"sequence", String(R"(const w = value => ({type:'Card', children:[
         {type:'Text',id:'label',value,streaming:true}]});
         streamWidget(w('北')); progress('step'); streamWidget(w('北京'));
         streamWidget(w('京')); text({state:'done'}))")},
       {"duplicate", String(R"(streamWidget({type:'Basic',children:[
         {type:'Text',id:'t',value:'A'},{type:'Text',id:'t',value:'B'}]});
         text({state:'wrong'}))")},
       {"invalid_frame", String(R"(streamWidget({type:'Card',children:[]});
         streamWidget({type:'Button'});text({state:'wrong'}))")},
       {"after_error", String(R"(streamWidget({type:'Card',children:[]});
         text({title:1,state:'wrong'}))")},
       {"plain_widget", String(R"(streamWidget({type:'Card',children:[]}))")},
       {"final_widget", String(R"(streamWidget({type:'Card',children:[]});
         text({widget:{type:'Basic',children:[]}}))")},
       {"wire_limit", String(R"(for(let i=0;i<10;i++)
         streamWidget({type:'Card',meta:'a'.repeat(60000),n:i}))")},
       {"live_bad",
        String("progress('checking');text({title:8,remove_sender:true})")},
       {"bad_unicode",
        String("progress(String.fromCharCode(0xd800));text({})")},
       {"many", String("for(let i=0;i<65;i++) progress('x');text({})")},
       {"invalid",
        String("text({state:'wrong',widget:{type:'Card',children:[17]}})")},
       {"read", String("text({effects:[{name:'read',data:{answer:actionContext."
                       "state}}]})")}});
  const auto definition =
      Object({{"id", workflow}, {"chatkit_actions", handlers}});
  std::string sealed;
  Q4T_CHECK(StateCipher(objects)
                .Seal(OutputJson(definition),
                      {"q4t.chatkit", "workflow", "snapshot", grant.session},
                      access, &sealed)
                .ok());
  Set(&grant.data, "workflow_snapshot", String(sealed));
  ObjectRequest put;
  put.scope = access.scope;
  put.operation = ObjectOperation::kPut;
  put.kind = ObjectKind::kChatKitThread;
  put.id = "cthr_actions";
  put.data = Object(
      {{"thread", Object({{"id", String(put.id)},
                          {"user", String("reader")},
                          {"object", String("chatkit.thread")},
                          {"created_at", Number(now)},
                          {"title", {}},
                          {"status", Object({{"type", String("active")}})}})},
       {"items", Array()},
       {"workflow", workflow},
       {"created_by", String(grant.session)}});
  Q4T_CHECK(objects.Execute(put, access.deadline, {}).ok());
  auto dispatch = [&](const StateAccess& who, std::string name, Json payload,
                      Json sender, bool streaming,
                      const std::function<bool(const Json&)>& progress = {}) {
    auto params = Object({{"thread_id", String(put.id)},
                          {"item_id", sender},
                          {"action", Object({{"type", String(std::move(name))},
                                             {"payload", payload}})}});
    return chatkit_detail::DispatchAction(objects, who, grant, params,
                                          streaming, progress);
  };
  Q4T_CHECK(
      dispatch(Access("client:other"), "show", {}, {}, false).http_status ==
      404);
  Q4T_CHECK(dispatch(access, "unknown", {}, {}, false).http_status == 404);
  const auto shown = dispatch(access, "show", String("record B"), {}, false);
  Q4T_CHECK(shown.ok());
  const auto& widget = *shown.value.Find("updated_item");
  Q4T_CHECK(widget.GetString("type") == "widget" &&
            widget.Find("widget")->IsObject());
  const auto id = *widget.Find("id");
  Q4T_CHECK(dispatch(access, "invalid", {}, id, false).http_status == 502);
  Q4T_CHECK(dispatch(access, "read", {}, id, false).http_status == 502);
  auto check = dispatch(access, "read", {}, id, true);
  Q4T_CHECK(check.ok() && check.value.array.size() == 1 &&
            check.value.array.front().Find("data")->GetString("answer") ==
                "record B");
  Q4T_CHECK(
      dispatch(access, "show", {}, String("item_missing"), true).http_status ==
      404);
  auto next = dispatch(access, "show", Number(2), id, true);
  Q4T_CHECK(next.ok() && next.value.array.size() == 1 &&
            next.value.array.front().GetString("type") ==
                "thread.item.replaced" &&
            next.value.array.front().Find("item")->GetString("id") == id.str);
  std::vector<std::string> progress;
  auto observe = [&](const Json& event) {
    if (event.GetString("type") != "progress_update") return false;
    progress.push_back(event.GetString("text"));
    return true;
  };
  Q4T_CHECK(dispatch(access, "live_bad", {}, id, true, observe).http_status ==
            502);
  Q4T_CHECK(progress == std::vector<std::string>{"checking"});
  auto cancelled = dispatch(access, "finish", {}, id, true,
                            [](const Json&) { return false; });
  Q4T_CHECK(cancelled.http_status == 499);
  progress.clear();
  Q4T_CHECK(dispatch(access, "many", {}, id, true, observe).http_status ==
                502 &&
            progress.size() == 64);
  Q4T_CHECK(dispatch(access, "finish", {}, id, false).http_status == 502);
  progress.clear();
  Q4T_CHECK(
      dispatch(access, "bad_unicode", {}, id, true, observe).http_status ==
          502 &&
      progress.empty());
  // The worker is still waiting for acknowledgment. A real intervening
  // thread write must win without erasing the widget.
  bool renamed = false;
  auto race = dispatch(access, "finish", {}, id, true, [&](const Json&) {
    auto params = Object(
        {{"thread_id", String(put.id)}, {"title", String("Other writer")}});
    renamed = chatkit_detail::BrowserOperation(objects, access, grant,
                                               "threads.update", params)
                  .ok();
    return renamed;
  });
  Q4T_CHECK(renamed && race.http_status == 409);
  auto after_bad = chatkit_detail::ReadThread(objects, access, put.id, &grant);
  Q4T_CHECK(after_bad.ok() &&
            after_bad.value.Find("data")->GetArray("items")->array.size() == 1);
  progress.clear();
  auto finish = dispatch(access, "finish", {}, id, true, observe);
  Q4T_CHECK(finish.ok() && progress == std::vector<std::string>{"checking"});
  const auto& events = finish.value.array;
  Q4T_CHECK(events.size() == 3 && events[0].GetString("type") == "notice" &&
            events[1].GetString("type") == "thread.item.removed" &&
            events[1].GetString("item_id") == id.str &&
            events[2].GetString("type") == "thread.updated");
  auto saved = chatkit_detail::ReadThread(objects, access, put.id, &grant);
  Q4T_CHECK(saved.ok());
  const auto thread = chatkit_detail::ClientThread(*saved.value.Find("data"));
  Q4T_CHECK(thread.GetString("title") == "Reviewed" &&
            thread.Find("items")->GetArray("data")->array.empty() &&
            OutputJson(thread) == OutputJson(*events[2].Find("thread")));
  std::vector<Json> live;
  auto collect = [&](const Json& event) {
    live.push_back(event);
    return true;
  };
  auto item_count = [&]() {
    auto record = chatkit_detail::ReadThread(objects, access, put.id, &grant);
    return record.value.Find("data")->GetArray("items")->array.size();
  };
  Q4T_CHECK(
      dispatch(access, "invalid_frame", {}, {}, true, collect).http_status ==
      502);
  Q4T_CHECK(live.size() == 1 && item_count() == 0);
  live.clear();
  Q4T_CHECK(
      dispatch(access, "after_error", {}, {}, true, collect).http_status ==
      502);
  Q4T_CHECK(live.size() == 1 && item_count() == 0);
  live.clear();
  Q4T_CHECK(dispatch(access, "duplicate", {}, {}, true, collect).http_status ==
            502);
  Q4T_CHECK(live.empty() && item_count() == 0);
  Q4T_CHECK(dispatch(access, "wire_limit", {}, {}, true, collect).http_status ==
            502);
  Q4T_CHECK(live.size() == 8 && item_count() == 0);
  Q4T_CHECK(dispatch(access, "sequence", {}, {}, true, [](const Json&) {
              return false;
            }).http_status == 499);
  Q4T_CHECK(item_count() == 0);
  live.clear();
  const auto seq = dispatch(access, "sequence", {}, {}, true, collect);
  Q4T_CHECK(seq.ok() && live.size() == 4 && seq.value.array.size() == 1);
  const auto new_id = *live[0].Find("item")->Find("id");
  Q4T_CHECK(live[0].GetString("type") == "thread.item.added" &&
            live[1].GetString("text") == "step" &&
            live[2].GetString("type") == "thread.item.updated" &&
            live[2].GetString("item_id") == new_id.str &&
            live[3].GetString("item_id") == new_id.str);
  const auto& delta = *live[2].Find("update");
  Q4T_CHECK(delta.GetString("type") == "widget.streaming_text.value_delta" &&
            delta.GetString("component_id") == "label" &&
            delta.GetString("delta") == "京" && !delta.GetBool("done", true));
  Q4T_CHECK(live[3].Find("update")->GetString("type") == "widget.root.updated");
  const auto& complete = seq.value.array.front();
  Q4T_CHECK(complete.GetString("type") == "thread.item.done" &&
            complete.Find("item")->GetString("id") == new_id.str);
  auto latest = chatkit_detail::ReadThread(objects, access, put.id, &grant);
  const auto& record = *latest.value.Find("data");
  Q4T_CHECK(record.GetString("action_state") == "done" &&
            record.GetArray("items")->array.size() == 1 &&
            OutputJson(chatkit_detail::ClientItem(
                record.GetArray("items")->array.front())) ==
                OutputJson(*complete.Find("item")));
  live.clear();
  const auto changed =
      dispatch(access, "final_widget", {}, new_id, true, collect);
  Q4T_CHECK(changed.ok() && live.size() == 2 &&
            changed.value.array.size() == 1);
  Q4T_CHECK(live[0].GetString("type") == "thread.item.updated" &&
            live[1].GetString("item_id") == new_id.str &&
            changed.value.array.front().GetString("type") ==
                "thread.item.replaced" &&
            changed.value.array.front().Find("item")->Find("widget")->GetString(
                "type") == "Basic");
  live.clear();
  auto only = dispatch(access, "plain_widget", {}, {}, true, collect);
  Q4T_CHECK(only.ok() && live.size() == 1 && only.value.array.size() == 1 &&
            item_count() == 2);
  auto conflicted =
      dispatch(access, "sequence", {}, {}, true, [&](const Json& ev) {
        if (ev.GetString("type") != "thread.item.added") return true;
        return chatkit_detail::BrowserOperation(
                   objects, access, grant, "threads.update",
                   Object({{"thread_id", String(put.id)},
                           {"title", String("Concurrent")}}))
            .ok();
      });
  Q4T_CHECK(conflicted.http_status == 409 && item_count() == 2);
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
Q4T_TEST(chatkit_registered_web_search_preserves_scope_and_access_mode) {
  auto declaration =
      Object({{"type", String("web_search")},
              {"mode", String("cached")},
              {"context_size", String("low")},
              {"allowed_domains", Array({String("example.org")})},
              {"location", Object({{"country", String("CN")}})}});
  const auto original = OutputJson(declaration);
  const auto cached = NativeAgentWebSearch(declaration);
  Q4T_CHECK(!cached.GetBool("external_web_access", true) &&
            cached.GetString("search_context_size") == "low");
  Q4T_CHECK(cached.Find("filters")->GetArray("allowed_domains")->array[0].str ==
            "example.org");
  Q4T_CHECK(cached.Find("user_location")->GetString("type") == "approximate" &&
            cached.Find("user_location")->GetString("country") == "CN");
  Q4T_CHECK(original == OutputJson(declaration));
  Set(&declaration, "mode", String("live"));
  Q4T_CHECK(NativeAgentWebSearch(declaration).GetBool("external_web_access"));
  Set(&declaration, "mode", String("disabled"));
  Q4T_CHECK(NativeAgentWebSearch(declaration).IsNull());
  return true;
}

Q4T_TEST(chatkit_cancelled_generation_keeps_only_nonempty_assistant_output) {
  Json response;
  Q4T_CHECK(io::ParseJson(R"({
    "status":"cancelled","output":[
    {"id":"fc1","type":"function_call","arguments":"{","name":"search"},
    {"id":"msg_empty","type":"message","role":"assistant","content":[
      {"type":"output_text","text":" \u0085\u00a0\u2002\u3000"}]},
    {"id":"msg_partial","type":"message","role":"assistant","content":[
      {"type":"output_text","text":"部分𐀀"}]},
    {"id":"msg_refusal","type":"message","role":"assistant","content":[
      {"type":"refusal","refusal":"declined"}]}]})",
                          &response));
  auto projected = chatkit_detail::ProjectOutput(response, "cthr_job");
  Q4T_CHECK(projected.ok() && projected.value.array.size() == 2);
  Q4T_CHECK(projected.value.array[0].GetString("id") == "msg_partial" &&
            projected.value.array[1].GetString("id") == "msg_refusal");
  Set(&response, "status", String("completed"));
  Q4T_CHECK(!chatkit_detail::ProjectOutput(response, "cthr_job").ok());
  return true;
}

Q4T_TEST(chatkit_generation_preserves_browser_and_management_citation_sources) {
  Json response;
  Q4T_CHECK(io::ParseJson(R"({
    "created_at":1,"output":[{"type":"message","role":"assistant","id":"msg_1",
    "content":[{"type":"output_text","text":"出处𐀀","annotations":[
      {"type":"file_citation","filename":"发布.md","file_id":"f","index":0},
      {"type":"file_citation","filename":"","index":1},
      {"type":"file_path","file_id":"other","index":2},
      {"type":"url_citation","title":"发布","url":"https://q4t.example/r",
       "start_index":1,"end_index":3},
      {"type":"container_file_citation","filename":"验收.log","start_index":2,
       "end_index":3}]},{"type":"refusal","refusal":"refused"}]}]})",
                          &response));
  auto projected = chatkit_detail::ProjectOutput(response, "cthr_1");
  Q4T_CHECK(projected.ok() && projected.value.array.size() == 1);
  auto management = projected.value.array.front();
  auto browser = chatkit_detail::ClientItem(management);
  const auto& content = browser.GetArray("content")->array;
  const auto& refs = content[0].GetArray("annotations")->array;
  Q4T_CHECK(refs.size() == 3 &&
            content[1].GetArray("annotations")->array.empty());
  Q4T_CHECK(refs[0].GetString("type") == "annotation" &&
            refs[0].GetInt("index", -1) == 0 &&
            refs[0].Find("source")->GetString("filename") == "发布.md" &&
            refs[0].Find("source")->GetString("title") == "发布.md");
  Q4T_CHECK(refs[1].Find("source")->GetString("type") == "url" &&
            refs[1].Find("source")->GetString("title") == "发布" &&
            refs[1].GetInt("index") == 3);
  Q4T_CHECK(refs[2].Find("source")->GetString("filename") == "验收.log" &&
            refs[2].GetInt("index") == 3);
  chatkit_detail::ProjectManagementItem(&management);
  const auto& compact =
      management.GetArray("content")->array[0].GetArray("annotations")->array;
  Q4T_CHECK(compact.size() == 3 && compact[0].GetString("type") == "file" &&
            compact[0].Find("source")->GetString("filename") == "发布.md" &&
            !compact[0].Has("index") &&
            !compact[0].Find("source")->Has("title"));
  Q4T_CHECK(compact[1].GetString("type") == "url" &&
            compact[1].Find("source")->GetString("url") ==
                "https://q4t.example/r");
  return true;
}

Q4T_TEST(chatkit_reasoning_workflow_updates_only_public_summaries) {
  using chatkit_detail::GenerationStream;
  Json response;
  Q4T_CHECK(io::ParseJson(R"({
    "created_at":7,"output":[
      {"type":"reasoning","id":"rs_a","encrypted_content":"hidden encrypted",
       "content":[{"type":"reasoning_text","text":"hidden plain"}],
       "summary":[{"type":"summary_text","text":"公开摘要"}]},
      {"type":"reasoning","id":"rs_b","summary":[
        {"type":"summary_text","text":"second"}]},
      {"type":"message","id":"msg_m","role":"assistant","content":[
        {"type":"output_text","text":"42","annotations":[]}]}]})",
                          &response));
  const auto& raw = response.GetArray("output")->array;
  const auto times = Object(
      {{"rs_a", Number(10)}, {"rs_b", Number(11)}, {"msg_m", Number(14)}});
  auto projected =
      chatkit_detail::ProjectOutput(response, "cthr_a", {}, &times);
  Q4T_CHECK(projected.ok() && projected.value.array.size() == 2);
  auto group = projected.value.array[0];
  const auto browser = chatkit_detail::ClientItem(group);
  Q4T_CHECK(browser.GetString("type") == "workflow" &&
            browser.GetString("id") == "wf_rs_a" &&
            !browser.Has("workflow_closed"));
  const auto* workflow = browser.Find("workflow");
  Q4T_CHECK(workflow->Find("summary")->GetInt("duration") == 4 &&
            workflow->GetArray("tasks")->array.size() == 2);
  chatkit_detail::ProjectManagementItem(&group);
  Q4T_CHECK(group.GetString("type") == "chatkit.task_group" &&
            !group.Has("workflow") && !group.Has("workflow_closed"));
  const auto& tasks = group.GetArray("tasks")->array;
  Q4T_CHECK(tasks.size() == 2 && tasks[0].GetString("summary") == "公开摘要" &&
            tasks[1].GetString("summary") == "second");
  GenerationStream stream;
  Json observed = Array();
  auto emit = [&](const Json& event) {
    observed.array.push_back(event);
    return true;
  };
  const auto item_event = [&](std::string type, const Json& item) {
    return stream.EmitEvent(
        Object({{"type", String(std::move(type))}, {"item", item}}), "cthr_a",
        7, &times, emit);
  };
  const auto part_event = [&](const std::string& type, const std::string& id,
                              const std::string& text) {
    return stream.EmitEvent(
        Object({{"type", String(type)},
                {"item_id", String(id)},
                {"summary_index", Number(0)},
                {type.ends_with(".delta") ? "delta" : "text", String(text)}}),
        "cthr_a", 7, &times, emit);
  };
  Q4T_CHECK(item_event("response.output_item.added", raw[0]));
  Q4T_CHECK(part_event("response.reasoning_text.delta", "rs_a", "hidden raw"));
  Q4T_CHECK(
      part_event("response.reasoning_summary_text.delta", "rs_a", "公开"));
  Q4T_CHECK(
      part_event("response.reasoning_summary_text.done", "rs_a", "公开摘要"));
  Q4T_CHECK(item_event("response.output_item.added", raw[1]));
  Q4T_CHECK(part_event("response.reasoning_summary_text.delta", "rs_b", "sec"));
  Q4T_CHECK(
      part_event("response.reasoning_summary_text.done", "rs_b", "second"));
  Q4T_CHECK(item_event("response.output_item.added", raw[2]));
  Q4T_CHECK(part_event("response.reasoning_summary_text.done", "rs_b", "late"));
  Q4T_CHECK(item_event("response.output_item.done", raw[2]));
  Q4T_CHECK(item_event("response.output_item.done", raw[2]));
  Q4T_CHECK(observed.array.size() == 7 && stream.FinishedMessage("msg_m"));
  Q4T_CHECK(observed.array[1].Find("update")->GetString("type") ==
            "workflow.task.added");
  Q4T_CHECK(observed.array[2].Find("update")->GetString("type") ==
            "workflow.task.updated");
  Q4T_CHECK(observed.array[3].Find("update")->GetInt("task_index") == 1);
  Q4T_CHECK(observed.array[4].GetString("type") == "thread.item.done" &&
            OutputJson(*observed.array[4].Find("item")) == OutputJson(browser));
  Q4T_CHECK(observed.array[5].GetString("type") == "thread.item.added" &&
            observed.array[6].GetString("type") == "thread.item.done");
  Q4T_CHECK(OutputJson(observed).find("hidden") == std::string::npos &&
            OutputJson(group).find("hidden") == std::string::npos &&
            OutputJson(browser).find("hidden") == std::string::npos);
  return true;
}

Q4T_TEST(
    chatkit_open_workflow_resumes_before_original_tool_and_drops_pending_cancel) {
  Json response;
  Q4T_CHECK(io::ParseJson(R"({"created_at":12,"output":[
    {"id":"fc_1","type":"function_call","call_id":"call_1","name":"read","arguments":"{}"},
    {"id":"rs_1","type":"reasoning","summary":[{"type":"summary_text","text":"first"}]}
  ]})",
                          &response));
  auto first = chatkit_detail::ProjectOutput(response, "cthr_a");
  Q4T_CHECK(first.ok() && first.value.array.size() == 2);
  const auto* seed = chatkit_detail::ResumableWorkflow(first.value);
  Q4T_CHECK(seed && seed->GetString("id") == "wf_rs_1" &&
            first.value.array.back().GetString("type") ==
                "chatkit.client_tool_call");
  auto next = response;
  auto& output = Field(next, "output").array;
  output.erase(output.begin());
  Set(&output.front(), "id", String("rs_next"));
  Set(&Field(output.front(), "summary").array.front(), "text", String("new"));
  const auto open =
      chatkit_detail::ProjectOutput(next, "cthr_a", {}, nullptr, seed);
  Q4T_CHECK(
      open.ok() && open.value.array.size() == 1 &&
      open.value.array[0].GetString("id") == seed->GetString("id") &&
      open.value.array[0].Find("workflow")->GetArray("tasks")->array.size() ==
          2);
  Set(&next, "status", String("cancelled"));
  const auto cancelled =
      chatkit_detail::ProjectOutput(next, "cthr_a", {}, nullptr, seed);
  Q4T_CHECK(cancelled.ok() && cancelled.value.array.empty());
  Json msg;
  Q4T_CHECK(io::ParseJson(R"({"type":"message","role":"assistant","id":"msg_1",
    "content":[{"type":"output_text","text":"offered","annotations":[]}]})",
                          &msg));
  Field(next, "output").array.push_back(msg);
  const auto closed =
      chatkit_detail::ProjectOutput(next, "cthr_a", {}, nullptr, seed);
  Q4T_CHECK(closed.ok() && closed.value.array.size() == 2);
  Q4T_CHECK(closed.value.array[0].GetBool("workflow_closed") &&
            closed.value.array[0].Find("workflow")->Find("summary")->IsNull());
  Q4T_CHECK(chatkit_detail::ResumableWorkflow(closed.value) == nullptr);
  return true;
}

Q4T_TEST(chatkit_generation_numbers_only_visible_citations_per_item_and_part) {
  chatkit_detail::GenerationStream stream;
  const auto project = [&](const Json& e) {
    Json output;
    stream.EmitEvent(e, "cthr_1", 1, nullptr, [&](const Json& item) {
      output = item;
      return true;
    });
    return output;
  };
  auto event =
      Object({{"type", String("response.output_text.annotation.added")},
              {"item_id", String("msg_a")},
              {"content_index", Number(0)},
              {"annotation_index", Number(9)}});
  auto raw = Object({{"type", String("file_citation")},
                     {"filename", String("")},
                     {"index", Number(7)}});
  Set(&event, "annotation", raw);
  Q4T_CHECK(project(event).IsNull());
  Set(&raw, "filename", String("run.md"));
  Set(&event, "annotation", raw);
  auto check = [&](const char* item, int part, int next) {
    Set(&event, "item_id", String(item));
    Set(&event, "content_index", Number(part));
    auto projected = project(event);
    const auto* update = projected.Find("update");
    return projected.GetString("type") == "thread.item.updated" &&
           projected.GetString("item_id") == item && update &&
           update->GetString("type") ==
               "assistant_message.content_part.annotation_added" &&
           update->GetInt("content_index") == part &&
           update->GetInt("annotation_index", -1) == next &&
           update->Find("annotation")->GetInt("index") == 7;
  };
  Q4T_CHECK(check("msg_a", 0, 0) && check("msg_b", 0, 0) &&
            check("msg_a", 1, 0) && check("msg_a", 0, 1));
  Set(&event, "type", String("response.content_part.done"));
  Set(&event, "part",
      Object({{"type", String("output_text")},
              {"text", String("answer")},
              {"annotations", Array({raw})}}));
  const auto done = project(event);
  const auto* part = done.Find("update")->Find("content");
  Q4T_CHECK(part->GetArray("annotations")->array.at(0).GetInt("index") == 7);
  return true;
}

Q4T_TEST(chatkit_browser_exceptions_emit_one_valid_terminal_even_without_heap) {
  for (int variant : {0, 1, 2}) {
    int fd[2];
    Q4T_CHECK(socketpair(AF_UNIX, SOCK_STREAM, 0, fd) == 0);
    detail::HttpResponseContext http(fd[0]);
    http.SetChatKitStream();
    Q4T_CHECK(detail::BeginStreamingResponse(fd[0], http.State()));
    if (variant == 0)
      detail::SendError(fd[0], 400, "invalid input");
    else
      detail::SendExceptionResponse(fd[0], *http.State(),
                                    variant == 1
                                        ? detail::ExceptionKind::kHostMemory
                                        : detail::ExceptionKind::kInternal);
    detail::SendExceptionResponse(fd[0], *http.State());
    char bytes[4096];
    const auto n = recv(fd[1], bytes, sizeof(bytes), MSG_DONTWAIT);
    close(fd[0]);
    close(fd[1]);
    Q4T_CHECK(n > 0 && *http.State() == detail::ResponseState::kComplete);
    const std::string wire(bytes, n);
    const auto start = wire.find("data: ");
    Q4T_CHECK(start != wire.npos &&
              wire.find("data: ", start + 1) == wire.npos);
    Json event;
    Q4T_CHECK(io::ParseJson(wire.substr(start + 6), &event) &&
              event.GetString("type") == "error" &&
              event.GetString("code") ==
                  (variant ? "stream.error" : "custom") &&
              event.GetBool("allow_retry") == (variant != 0));
  }
  return true;
}

Q4T_TEST(chatkit_generation_errors_finish_only_a_delivered_browser_stream) {
  for (bool connected : {true, false}) {
    int fd[2];
    Q4T_CHECK(socketpair(AF_UNIX, SOCK_STREAM, 0, fd) == 0);
    detail::HttpResponseContext http(fd[0]);
    Q4T_CHECK(detail::BeginStreamingResponse(fd[0], http.State()));
    int terminals = 0, status = 0;
    http.SetBeforeTerminal([](void* p) noexcept { ++*static_cast<int*>(p); },
                           &terminals);
    http.SetErrorObserver(
        [](void* p, int s, std::string_view, std::string_view) noexcept {
          *static_cast<int*>(p) = s;
        },
        &status);
    if (!connected) shutdown(fd[1], SHUT_RDWR);
    chatkit_detail::FinishStreamError(
        fd[0], Access(),
        Error(503, "chatkit_thread_invalid", "Invalid stored ChatKit thread"),
        true);
    const auto state = *http.State();
    std::string wire;
    if (connected) {
      char buf[4096];
      const auto size = recv(fd[1], buf, sizeof(buf), MSG_DONTWAIT);
      if (size > 0) wire.assign(buf, size);
    }
    close(fd[0]);
    close(fd[1]);
    Q4T_CHECK(terminals == 1 && status == 503);
    Q4T_CHECK(state == (connected ? detail::ResponseState::kComplete
                                  : detail::ResponseState::kTerminal));
    if (connected) {
      const auto pos = wire.find("data: ");
      Q4T_CHECK(pos != wire.npos && wire.find("data: ", pos + 1) == wire.npos);
      Json event;
      Q4T_CHECK(io::ParseJson(wire.substr(pos + 6), &event));
      Q4T_CHECK(event.GetString("type") == "error" &&
                event.GetString("message") == "Invalid stored ChatKit thread" &&
                event.GetBool("allow_retry"));
    }
  }
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
Q4T_TEST(chatkit_image_attachment_resolves_original_pixels_and_scoped_file) {
  Fixture fixture;
  ObjectStore objects(fixture.options);
  const auto access = Access();
  const BinaryAccess binary{access.scope, access.deadline, access.cancelled};
  auto session = Create(objects, access, 16, true);
  chatkit_detail::Grant owner;
  Q4T_CHECK(session.ok() &&
            chatkit_detail::Redeem(objects, access,
                                   session.value.GetString("client_secret"),
                                   std::time(nullptr), &owner)
                .ok());
  BinaryStore store(objects);
  const auto images =
      std::filesystem::path(__FILE__).parent_path() / "fixtures/file_images";
  for (const auto& [name, mime] :
       {std::pair{"original.png", "image/png"},
        std::pair{"original.jpg", "image/jpeg"},
        std::pair{"gif/opaque.gif", "image/gif"},
        std::pair{"original.png", "application/octet-stream"}}) {
    std::ifstream input(images / name, std::ios::binary);
    Q4T_CHECK(input);
    std::string bytes(std::istreambuf_iterator<char>(input), {});
    BinaryStage stage;
    Q4T_CHECK(store.Begin(binary, 1U << 20, 60, &stage).ok() &&
              store.Append(binary, &stage, bytes).ok());
    auto upload = chatkit_detail::PublishAttachment(objects, binary, owner,
                                                    &stage, name, mime);
    Q4T_CHECK(upload.ok());
    const auto id = upload.value.GetString("id");
    const auto ids = Array({String(id)});
    auto visible = Array(), model = Array();
    auto other = owner;
    other.session += "x";
    Q4T_CHECK(chatkit_detail::ResolveAttachments(objects, access, other, ids,
                                                 &visible, &model)
                  .http_status == 404);
    Q4T_CHECK(chatkit_detail::ResolveAttachments(objects, access, owner, ids,
                                                 &visible, &model)
                  .ok());
    const auto preview = upload.value.GetString("preview_url");
    Q4T_CHECK(upload.value.GetString("type") == "image" &&
              preview.starts_with("data:image/png;base64,") &&
              visible.array.size() == 1 &&
              visible.array[0].GetString("preview_url") == preview &&
              visible.array[0].GetString("type") == "image" &&
              !visible.array[0].Has("upload_descriptor"));
    Q4T_CHECK(model.array.size() == 1 &&
              model.array[0].GetString("type") == "input_image" &&
              model.array[0].GetString("file_id") == id);
    ChatPlanError error_shape;
    Q4T_CHECK(ValidateResponsesItemShape(
        Object({{"role", String("user")}, {"content", model}}),
        ResponsesItemShape::kInput, &error_shape, "input[0]"));
    FileInputSource source;
    Q4T_CHECK(ParseImageInputSource(model.array[0],
                                    FileInputProfile::kResponses,
                                    "input[0].content[0]", &source)
                  .ok());
    FileInputBudget budget;
    FileImageInput image;
    Q4T_CHECK(
        ReadFileImage(&objects, binary, source, {}, {}, &budget, &image).ok() &&
        image.source.bytes == bytes && image.source.file_id == id &&
        image.header.width == 64 && image.header.height == 32);
    vision::ProcessorConfig config;
    config.min_pixels = config.max_pixels = 2048;
    vision::ProcessedImage pixels;
    std::string error;
    Q4T_CHECK(vision::ProcessImage(
        reinterpret_cast<const uint8_t*>(image.source.bytes.data()),
        image.source.bytes.size(), config, &pixels, &error));
    Q4T_CHECK(pixels.grid_w == 4 && pixels.grid_h == 2 &&
              pixels.pixel_values.size() == 8 * 3 * 2 * 16 * 16);
    Q4T_CHECK(
        chatkit_detail::RemoveAttachment(objects, access, owner, id).ok());
  }
  return true;
}

Q4T_TEST(
    chatkit_preview_preserves_alpha_aspect_ratio_and_rejects_invalid_images) {
  const auto root = std::filesystem::path(__FILE__).parent_path() /
                    "fixtures/file_images/webp";
  const auto read = [&](const char* file) {
    std::ifstream input(root / file, std::ios::binary);
    return std::string(std::istreambuf_iterator<char>(input), {});
  };
  BinaryAccess access{
      "client:alpha", std::chrono::steady_clock::now() + 10s, {}};
  for (const auto& [file, expected_height] :
       {std::pair{"alpha.webp", 64U}, std::pair{"noise.webp", 48U}}) {
    std::string preview;
    Q4T_CHECK(MakeRasterPreview(read(file), access, &preview).ok());
    vision::MediaHeader header;
    const auto* data = reinterpret_cast<const uint8_t*>(preview.data());
    Q4T_CHECK(vision::ReadMediaHeader({data, preview.size()}, &header) &&
              header.format == vision::MediaFormat::kPng &&
              header.width == 96 && header.height == expected_height);
    std::vector<uint8_t> rgba;
    Q4T_CHECK(vision::DecodePreviewRgba(data, preview.size(), header.width,
                                        header.height, header.width,
                                        header.height, &rgba));
    Q4T_CHECK(rgba[3] == (expected_height == 64 ? 0 : 255));
  }
  std::string preview = "stale";
  Q4T_CHECK(
      MakeRasterPreview(read("animated.webp"), access, &preview).http_status ==
          400 &&
      preview.empty());
  auto broken = read("alpha.png");
  broken.resize(40);  // Valid IHDR, missing compressed pixels.
  Q4T_CHECK(MakeRasterPreview(broken, access, &preview).http_status == 400 &&
            preview.empty());
  Q4T_CHECK(MakeRasterPreview("<svg/>", access, &preview).http_status == 400);
  access.cancelled = [] { return true; };
  Q4T_CHECK(
      MakeRasterPreview(read("alpha.png"), access, &preview).http_status ==
          499 &&
      preview.empty());
  return true;
}

Q4T_TEST(
    chatkit_preview_capacity_keeps_upload_and_model_admission_independent) {
  Fixture fixture;
  ObjectStore objects(fixture.options);
  const auto access = Access();
  const BinaryAccess binary{access.scope, access.deadline, access.cancelled};
  auto session = Create(objects, access, 16, true);
  chatkit_detail::Grant owner;
  Q4T_CHECK(session.ok() &&
            chatkit_detail::Redeem(objects, access,
                                   session.value.GetString("client_secret"),
                                   std::time(nullptr), &owner)
                .ok());
  // A real 32768 x 1 PNG fits the upload limit and exceeds the source
  // edge supported by both the previewer and model input.
  const auto path = std::filesystem::path(__FILE__).parent_path() /
                    "fixtures/file_images/preview-wide.png";
  std::ifstream input(path, std::ios::binary);
  Q4T_CHECK(input);
  const std::string original(std::istreambuf_iterator<char>(input), {});
  BinaryStore store(objects);
  BinaryStage stage;
  Q4T_CHECK(store.Begin(binary, 1U << 20, 60, &stage).ok() &&
            store.Append(binary, &stage, original).ok());
  auto upload = chatkit_detail::PublishAttachment(
      objects, binary, owner, &stage, "wide.png", "image/png");
  Q4T_CHECK(upload.ok() && upload.value.GetString("type") == "file");
  FileInputSource source;
  auto model = chatkit_detail::AttachmentModelInput(upload.value);
  Q4T_CHECK(ParseImageInputSource(model, FileInputProfile::kResponses,
                                  "input[0]", &source)
                .ok());
  FileInputBudget budget;
  FileImageInput image;
  Q4T_CHECK(ReadFileImage(&objects, binary, source, {}, {}, &budget, &image)
                .http_status == 413);
  return true;
}

#ifdef Q4T_GRAMMAR_PROCESS_TESTS
Q4T_TEST(chatkit_preview_codec_outage_keeps_scoped_image_input) {
  Fixture fixture;
  ObjectStore objects(fixture.options);
  const auto access = Access();
  const BinaryAccess binary{access.scope, access.deadline, access.cancelled};
  auto session = Create(objects, access, 16, true);
  chatkit_detail::Grant owner;
  Q4T_CHECK(session.ok() &&
            chatkit_detail::Redeem(objects, access,
                                   session.value.GetString("client_secret"),
                                   std::time(nullptr), &owner)
                .ok());
  const auto path = std::filesystem::path(__FILE__).parent_path() /
                    "fixtures/file_images/webp/lossless.webp";
  std::ifstream input(path, std::ios::binary);
  Q4T_CHECK(input);
  const std::string original(std::istreambuf_iterator<char>(input), {});
  BinaryStore store(objects);
  BinaryStage stage;
  Q4T_CHECK(store.Begin(binary, 1U << 20, 60, &stage).ok());
  Q4T_CHECK(
      store.Append(binary, &stage, original.substr(0, original.size() / 2))
          .ok());
  Q4T_CHECK(
      store.Append(binary, &stage, original.substr(original.size() / 2)).ok());
  std::string bytes = "stale";
  auto foreign = binary;
  foreign.scope = "client:other";
  Q4T_CHECK(!store.ReadStage(foreign, stage, original.size(), &bytes).ok() &&
            bytes.empty());
  Q4T_CHECK(
      store.ReadStage(binary, stage, original.size() - 1, &bytes).http_status ==
          413 &&
      bytes.empty());
  Q4T_CHECK(store.ReadStage(binary, stage, original.size(), &bytes).ok() &&
            bytes == original);
  detail::SetWebpWorkerEntryForTesting("--file-webp-missing-worker");
  ScopeExit reset([] { detail::SetWebpWorkerEntryForTesting({}); });
  auto upload = chatkit_detail::PublishAttachment(
      objects, binary, owner, &stage, "attachment.webp", "image/webp");
  Q4T_CHECK(upload.ok() && upload.value.GetString("type") == "file" &&
            !upload.value.Has("preview_url"));
  Q4T_CHECK(!store.ReadStage(binary, stage, original.size(), &bytes).ok() &&
            bytes.empty());
  auto visible = Array(), model = Array();
  const auto id = upload.value.GetString("id");
  Q4T_CHECK(chatkit_detail::ResolveAttachments(
                objects, access, owner, Array({String(id)}), &visible, &model)
                .ok());
  Q4T_CHECK(model.array[0].GetString("type") == "input_image" &&
            model.array[0].GetString("file_id") == id);
  Q4T_CHECK(chatkit_detail::RemoveAttachment(objects, access, owner, id).ok());
  auto reserved = chatkit_detail::ReserveAttachment(
      objects, access, owner,
      Object({{"name", String("recover.webp")},
              {"mime_type", String("image/webp")},
              {"size", Number(original.size())}}),
      {kBinding, kIssuer}, "https://example.test");
  Q4T_CHECK(reserved.ok());
  const auto read_url = reserved.value.GetString("preview_url");
  const auto mark = read_url.find("?ticket=");
  Q4T_CHECK(mark != read_url.npos);
  const auto ticket = read_url.substr(mark + 8);
  const auto reserved_id = reserved.value.GetString("id");
  const auto authorization = reserved.value.Find("upload_descriptor")
                                 ->Find("headers")
                                 ->GetString("Authorization");
  BinaryStage next;
  Q4T_CHECK(store.Begin(binary, 1U << 20, 60, &next).ok() &&
            store.Append(binary, &next, original).ok());
  auto put = chatkit_detail::PublishAttachment(
      objects, binary, owner, &next, "recover.webp", "image/webp", reserved_id,
      BearerCredential(authorization));
  Q4T_CHECK(put.ok() && put.value.GetString("type") == "file");
  auto read_preview = [&] {
    return chatkit_detail::ReadAttachmentPreview(objects, access, owner.session,
                                                 reserved_id, ticket, &bytes);
  };
  Q4T_CHECK(read_preview().http_status == 503 && bytes.empty());
  detail::SetWebpWorkerEntryForTesting({});
  Q4T_CHECK(read_preview().ok() && bytes.starts_with("\x89PNG"));
  return true;
}
#endif

Q4T_TEST(chatkit_retry_preserves_raster_and_legacy_document_identity) {
  const auto retry = [](const char* mime, const char* public_type,
                        const char* saved_type, const char* saved_id) {
    const auto attachment = Object({{"id", String("file_image")},
                                    {"name", String("asset")},
                                    {"mime_type", String(mime)},
                                    {"type", String(public_type)}});
    const auto user = Object({{"id", String("item_visible")},
                              {"type", String("chatkit.user_message")},
                              {"content", Array()},
                              {"attachments", Array({attachment})}});
    auto thread = Object({{"thread", Object({{"id", String("cthr_retry")}})},
                          {"last_response", String("resp_last")},
                          {"items", Array({user})}});
    auto part =
        Object({{"type", String(saved_type)}, {"file_id", String(saved_id)}});
    if (std::string_view(saved_type) == "input_image")
      Set(&part, "detail", String("auto"));
    const auto message = Object({{"id", String("item_model")},
                                 {"type", String("message")},
                                 {"role", String("user")},
                                 {"content", Array({part})}});
    const auto record =
        Object({{"chatkit_thread_id", String("cthr_retry")},
                {"response", Object({{"id", String("resp_last")}})},
                {"history", Array({message})}});
    chatkit_detail::RetryHistory history;
    Json selected;
    return chatkit_detail::SelectRetryHistory(record, "item_visible", &thread,
                                              &selected, &history)
               .ok() &&
           OutputJson(history.items) == OutputJson(Array({message}));
  };
  for (const auto* mime :
       {"image/png", "image/jpeg", "image/webp", "image/gif"}) {
    Q4T_CHECK(retry(mime, "file", "input_image", "file_image"));
    Q4T_CHECK(retry(mime, "file", "input_file", "file_image"));
    Q4T_CHECK(!retry(mime, "file", "input_image", "file_other"));
  }
  Q4T_CHECK(
      retry("IMAGE/PNG; profile=test", "file", "input_image", "file_image"));
  Q4T_CHECK(!retry("image/svg+xml", "file", "input_image", "file_image"));
  Q4T_CHECK(!retry("image/pngx", "file", "input_image", "file_image"));
  Q4T_CHECK(retry("application/pdf", "file", "input_file", "file_image"));
  Q4T_CHECK(!retry("application/pdf", "file", "input_image", "file_image"));
  Q4T_CHECK(!retry("image/png", "image", "input_file", "file_image"));
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

Q4T_TEST(
    chatkit_reserved_image_preview_has_independent_authority_and_lifetime) {
  Fixture fixture;
  ObjectStore objects(fixture.options);
  const auto access = Access();
  const BinaryAccess binary{access.scope, access.deadline, access.cancelled};
  auto session = Create(objects, access, 16, true);
  chatkit_detail::Grant owner;
  Q4T_CHECK(session.ok() &&
            chatkit_detail::Redeem(objects, access,
                                   session.value.GetString("client_secret"),
                                   std::time(nullptr), &owner)
                .ok());
  const auto fixture_path = std::filesystem::path(__FILE__).parent_path() /
                            "fixtures/file_images/original.png";
  std::ifstream input(fixture_path, std::ios::binary);
  Q4T_CHECK(input);
  const std::string bytes(std::istreambuf_iterator<char>(input), {});
  for (const bool revoke : {false, true}) {
    auto reserved = chatkit_detail::ReserveAttachment(
        objects, access, owner,
        Object({{"name", String("sample.png")},
                {"size", Number(bytes.size())},
                {"mime_type", String("image/png")}}),
        {kBinding, kIssuer}, "https://example.test");
    Q4T_CHECK(reserved.ok() && reserved.value.GetString("type") == "image");
    const auto id = reserved.value.GetString("id");
    const auto& descriptor = *reserved.value.Find("upload_descriptor");
    const auto url = reserved.value.GetString("preview_url");
    const auto base = descriptor.GetString("url") + "/preview?ticket=";
    Q4T_CHECK(url.starts_with(base));
    const std::string token = url.substr(base.size());
    const auto auth = descriptor.Find("headers")->GetString("Authorization");
    const auto upload = BearerCredential(auth);
    RealtimeClientTicket ticket;
    Q4T_CHECK(
        ReadRealtimeTicket(token, &ticket,
                           ClientTicketAudience::kChatKitPreview) &&
        VerifyRealtimeTicket(ticket, kBinding, kIssuer, std::time(nullptr)) &&
        !VerifyRealtimeTicket(ticket, kBinding, kIssuer, ticket.expires_at));
    Q4T_CHECK(!ReadRealtimeTicket(token, &ticket,
                                  ClientTicketAudience::kChatKitUpload) &&
              !ReadRealtimeTicket(upload, &ticket,
                                  ClientTicketAudience::kChatKitPreview));
    std::string result = "stale";
    auto get = [&](const StateAccess& who, std::string_view session_id,
                   std::string_view credential) {
      return chatkit_detail::ReadAttachmentPreview(objects, who, session_id, id,
                                                   credential, &result);
    };
    Q4T_CHECK(get(access, owner.session, token).http_status == 404 &&
              result.empty());
    chatkit_detail::ReservedFile file;
    Q4T_CHECK(chatkit_detail::ReadReservation(objects, access, &owner, id,
                                              token, &file)
                  .http_status == 401);
    BinaryStore store(objects);
    BinaryStage stage;
    Q4T_CHECK(store.Begin(binary, 1U << 20, 60, &stage).ok() &&
              store.Append(binary, &stage, bytes).ok());
    auto complete = chatkit_detail::PublishAttachment(
        objects, binary, owner, &stage, "sample.png", "image/png", id, upload);
    Q4T_CHECK(complete.ok() && complete.value.GetString("type") == "image" &&
              !complete.value.Has("upload_descriptor"));
    Q4T_CHECK(get(access, owner.session, token).ok() &&
              result.starts_with("\x89PNG"));
    std::string encoded;
    Q4T_CHECK(io::EncodeBase64(result, 64U << 10, &encoded) &&
              complete.value.GetString("preview_url") ==
                  "data:image/png;base64," + encoded);
    Q4T_CHECK(get(access, owner.session, upload).http_status == 404 &&
              result.empty());
    Q4T_CHECK(get(access, "cksess_other", token).http_status == 404);
    Q4T_CHECK(get(Access("client:beta"), owner.session, token).http_status ==
              404);
    if (revoke) {
      Q4T_CHECK(
          HandleChatKit(objects, access, "POST",
                        "/v1/chatkit/sessions/" + owner.session + "/cancel", "")
              .ok());
      Q4T_CHECK(get(access, owner.session, token).http_status == 401 &&
                result.empty());
    } else {
      Q4T_CHECK(
          chatkit_detail::RemoveAttachment(objects, access, owner, id).ok());
      Q4T_CHECK(get(access, owner.session, token).http_status == 404 &&
                result.empty());
    }
  }
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
