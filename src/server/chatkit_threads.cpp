#include "q4t/server/chatkit.h"

#include <map>
#include <unordered_set>

#include "assistant_threads_internal.h"
#include "chatkit_thread_state.h"
#include "chatkit_credentials.h"
#include "chatkit_attachments.h"
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
    return Error(503, "chatkit_thread_invalid",
                 "Invalid stored ChatKit thread");
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
  if (route.kind == Kind::kNone)
    return Error(404, "route_not_found", "Unknown ChatKit route");
  if (!ChatKitMethodAllowed(method, target)) {
    auto reply = Error(405, "method_not_allowed", "Method not allowed");
    reply.value =
        Object({{"allow", String(std::string(ChatKitAllow(route.kind)))}});
    return reply;
  }
  if (method == "POST")
    return HandleChatKitSession(objects, access, target, body, issuer);
  if (!body.empty()) return Invalid("body", "Unexpected request body");
  const auto split = target.find('?');
  if (split != target.npos && route.kind == Kind::kThread)
    return Invalid("query", "Unexpected query");
  const auto raw =
      split == target.npos ? std::string_view{} : target.substr(split + 1);
  auto query = ParseApplicationQuery(raw);
  if (!query.ok()) return query;
  if (route.kind != Kind::kThread) {
    ChatPlanError error;
    auto valid = ValidateChatKit(
        query.value,
        route.kind == Kind::kThreads ? "thread_query" : "item_query", &error);
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
    if (result.ok())
      result.value = Object({{"id", String(request.id)},
                             {"object", String("chatkit.thread.deleted")},
                             {"deleted", Boolean(true)}});
    return result;
  }
  if (route.kind == Kind::kThread) return thread;
  const auto* items = Field(prior.value, "data").GetArray("items");
  if (!items)
    return Error(503, "chatkit_thread_invalid", "Missing stored thread items");
  auto page = assistant_detail::PageAssistantRows(*items, query.value, true);
  if (!page.ok()) return page;
  ChatPlanError error;
  for (auto& item : Field(page.value, "data").array)
    chatkit_detail::ProjectManagementItem(&item);
  if (!ValidateChatKit(page.value, "items", &error))
    return Error(503, "chatkit_thread_invalid", "Invalid stored ChatKit items");
  return page;
}
namespace chatkit_detail {
namespace {
ObjectReply ClientPage(const Json& rows, const Json& params, bool threads) {
  auto query = Object();
  for (const auto* key : {"limit", "order", "after"}) {
    if (const auto* v = params.Find(key); v && !v->IsNull())
      Set(&query, key, *v);
  }
  ChatPlanError error;
  auto valid =
      ValidateChatKit(query, threads ? "thread_query" : "item_query", &error);
  if (!valid) return Invalid(error.param.c_str(), valid.message());
  auto page = assistant_detail::PageAssistantRows(rows, query);
  if (!page.ok()) return page;
  Json data = Array();
  for (const auto& row : page.value.GetArray("data")->array)
    data.array.push_back(threads ? ClientThread(Object({{"thread", row}}))
                                 : ClientItem(row));
  const auto more = page.value.GetBool("has_more");
  auto after = more ? Field(page.value, "last_id") : Json{};
  page.value = Object({{"data", std::move(data)},
                       {"has_more", Boolean(more)},
                       {"after", std::move(after)}});
  return page;
}
// Feedback belongs to the user/workflow-scoped thread. Keep it outside the
// public item objects so project and browser history retain their wire shape.
ObjectReply RecordFeedback(ObjectStore& objects, const StateAccess& access,
                           const Grant& grant, const Json& params) {
  const auto* thread = params.Find("thread_id");
  if (!thread || !thread->IsString())
    return Invalid("thread_id", "Expected a thread ID");
  const auto kind = params.GetString("kind");
  const auto* targets = params.GetArray("item_ids");
  if (!targets || (kind != "positive" && kind != "negative"))
    return Invalid("params", "Expected item IDs and a feedback kind");
  for (const auto& target : targets->array) {
    if (!target.IsString()) return Invalid("item_ids", "Expected an item ID");
  }
  ObjectReply accepted;
  accepted.value = Object();
  for (int attempt = 0; attempt < 4; ++attempt) {
    auto row = ReadThread(objects, access, thread->str, &grant);
    if (!row.ok()) return row;
    auto& data = Field(row.value, "data");
    std::unordered_set<std::string> items;
    for (const auto& item : data.GetArray("items")->array)
      items.insert(item.GetString("id"));
    std::map<std::string, std::string> votes;
    if (const auto* prior = data.Find("feedback")) {
      if (!prior->IsObject())
        return Error(503, "chatkit_thread_invalid", "Invalid stored feedback");
      for (const auto& [target, vote] : prior->object) {
        if (!vote.IsString() ||
            (vote.str != "positive" && vote.str != "negative"))
          return Error(503, "chatkit_thread_invalid",
                       "Invalid stored feedback");
        votes.emplace(target, vote.str);
      }
    }
    bool changed = false;
    for (const auto& target : targets->array) {
      if (!items.contains(target.str))
        return Error(404, "object_not_found", "Thread item not found");
      auto& vote = votes[target.str];
      if (vote != kind) {
        vote = kind;
        changed = true;
      }
    }
    if (!changed) return accepted;
    auto feedback = Object();
    for (const auto& [target, vote] : votes)
      feedback.object.emplace_back(target, String(vote));
    Set(&data, "feedback", std::move(feedback));
    ObjectRequest write;
    write.kind = ObjectKind::kChatKitThread;
    write.operation = ObjectOperation::kPut;
    write.scope = access.scope;
    write.id = thread->str;
    write.expected_revision = row.value.GetInt("revision");
    write.expires_at = row.value.GetInt("expires_at");
    write.data = std::move(data);
    auto result = objects.Execute(write, access.deadline, access.cancelled);
    if (result.ok()) return accepted;
    if (result.commit_unknown || result.code != "object_revision_conflict")
      return result;
  }
  return Error(409, "chatkit_thread_busy", "Thread changed during feedback");
}
}  // namespace

ObjectReply BrowserOperation(ObjectStore& objects, const StateAccess& access,
                             const Grant& grant, std::string_view operation,
                             const Json& params) {
  if (operation == "items.feedback")
    return RecordFeedback(objects, access, grant, params);
  if (operation == "attachments.delete")
    return RemoveAttachment(objects, access, grant,
                            params.GetString("attachment_id"));
  if (operation == "threads.list") {
    auto history = BrowserHistory(objects, access, grant);
    if (!history.ok()) return history;
    return ClientPage(history.value, params, true);
  }
  if (operation != "threads.get_by_id" && operation != "threads.update" &&
      operation != "threads.delete" && operation != "items.list")
    return Invalid("type", "Unknown ChatKit client operation");
  const auto id = params.GetString("thread_id");
  auto row = ReadThread(objects, access, id, &grant);
  if (!row.ok()) return row;
  auto& data = Field(row.value, "data");
  if (operation == "items.list")
    return ClientPage(*data.GetArray("items"), params, false);
  ObjectReply result;
  if (operation == "threads.get_by_id") {
    result.value = ClientThread(data);
    return result;
  }
  ObjectRequest write;
  write.kind = ObjectKind::kChatKitThread;
  write.scope = access.scope;
  write.id = id;
  write.expected_revision = row.value.GetInt("revision");
  if (operation == "threads.delete") {
    write.operation = ObjectOperation::kErase;
    result.value = Object();
  } else {
    const auto* title = params.Find("title");
    if (!title || !title->IsString())
      return Invalid("title", "Expected a thread title");
    Set(&Field(data, "thread"), "title", *title);
    result.value = ClientThread(data);
    write.operation = ObjectOperation::kPut;
    write.expires_at = row.value.GetInt("expires_at");
    write.data = std::move(data);
  }
  auto saved = objects.Execute(write, access.deadline, access.cancelled);
  return saved.ok() ? result : saved;
}
}  // namespace chatkit_detail
}  // namespace q4t::server
