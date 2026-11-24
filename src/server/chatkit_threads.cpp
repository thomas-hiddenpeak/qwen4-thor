#include "q4t/server/chatkit.h"

#include "assistant_threads_internal.h"
#include "chatkit_thread_state.h"
#include "object_protocol.h"
#include "q4t/server/application_query.h"

namespace q4t::server {
namespace {
using namespace object_detail;
using Kind = ChatKitRouteKind;

ObjectReply Invalid(const char* field, std::string message) {
  auto result = Error(400, "invalid_request", std::move(message));
  result.value = Object({{"param", String(field)}});
  return result;
}

ObjectReply Thread(const Json& record) {
  const auto* stored = record.Find("data");
  const auto* thread = stored ? stored->Find("thread") : nullptr;
  ChatPlanError error;
  if (!thread || record.GetString("id") != thread->GetString("id") ||
      !ValidateChatKit(*thread, "thread", &error))
    return Error(503, "chatkit_thread_invalid", "Invalid stored ChatKit thread");
  ObjectReply result;
  result.value = *thread;
  return result;
}

ObjectReply ListThreads(ObjectStore& objects, const StateAccess& access,
                        ObjectRequest scan, const Json& query) {
  scan.operation = ObjectOperation::kList;
  scan.limit = 100;
  scan.descending = false;
  const auto user = query.GetString("user");
  auto rows = Array();
  for (;;) {
    auto page = objects.Execute(scan, access.deadline, access.cancelled);
    if (!page.ok()) return page;
    const auto* records = page.value.GetArray("data");
    if (!records)
      return Error(503, "chatkit_thread_invalid", "Invalid stored thread page");
    for (const auto& record : records->array) {
      auto thread = Thread(record);
      if (!thread.ok()) return thread;
      if (!query.Has("user") || thread.value.GetString("user") == user)
        rows.array.push_back(std::move(thread.value));
    }
    if (!page.value.GetBool("has_more")) break;
    const auto end = page.value.GetString("last_id");
    if (end.empty() || end == scan.after)
      return Error(503, "chatkit_thread_invalid", "Invalid stored cursor");
    scan.after = end;
  }
  return assistant_detail::PageAssistantRows(rows, query, true);
}
}  // namespace

ObjectReply HandleChatKit(ObjectStore& objects, const StateAccess& access,
                          std::string_view method, std::string_view target,
                          const std::string& body, ChatKitIssuer issuer) {
  const auto route = ParseChatKitRoute(target);
  if (route.kind == Kind::kNone) return Error(404, "route_not_found", "Unknown ChatKit route");
  if (!ChatKitMethodAllowed(method, target)) {
    auto reply = Error(405, "method_not_allowed", "Method not allowed");
    reply.value = Object({{"allow", String(std::string(ChatKitAllow(route.kind)))}});
    return reply;
  }
  if (method == "POST") return HandleChatKitSession(objects, access, target, body, issuer);
  if (!body.empty()) return Invalid("body", "Unexpected request body");
  const auto split = target.find('?');
  if (split != target.npos && route.kind == Kind::kThread)
    return Invalid("query", "Unexpected query");
  const auto raw = split == target.npos ? std::string_view{} : target.substr(split + 1);
  auto query = ParseApplicationQuery(raw);
  if (!query.ok()) return query;
  if (route.kind != Kind::kThread) {
    ChatPlanError error;
    auto valid = ValidateChatKit(query.value,
                                 route.kind == Kind::kThreads ? "thread_query" : "item_query",
                                 &error);
    if (!valid) return Invalid(error.param.c_str(), valid.message());
  }
  ObjectRequest request;
  request.kind = ObjectKind::kChatKitThread;
  request.id = route.id;
  request.scope = access.scope;
  if (route.kind == Kind::kThreads)
    return ListThreads(objects, access, std::move(request), query.value);
  auto prior = chatkit_detail::ReadThread(objects, access, request.id);
  if (!prior.ok()) return prior;
  auto thread = Thread(prior.value);
  if (!thread.ok()) return thread;
  if (method == "DELETE") {
    request.operation = ObjectOperation::kErase;
    request.expected_revision = prior.value.GetInt("revision");
    auto result = objects.Execute(request, access.deadline, access.cancelled);
    if (result.ok()) result.value = Object({{"id", String(request.id)},
        {"object", String("chatkit.thread.deleted")}, {"deleted", Boolean(true)}});
    return result;
  }
  if (route.kind == Kind::kThread) return thread;
  const auto* items = Field(prior.value, "data").GetArray("items");
  if (!items) return Error(503, "chatkit_thread_invalid", "Missing stored thread items");
  auto page = assistant_detail::PageAssistantRows(*items, query.value, true);
  if (!page.ok()) return page;
  ChatPlanError error;
  if (!ValidateChatKit(page.value, "items", &error))
    return Error(503, "chatkit_thread_invalid", "Invalid stored ChatKit items");
  return page;
}
}  // namespace q4t::server
