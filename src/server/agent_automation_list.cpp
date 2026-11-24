#include "agent_automation_internal.h"

#include <algorithm>

#include "object_protocol.h"
#include "q4t/server/application_query.h"
#include "q4t/server/conversation_state.h"

namespace q4t::server::automation_detail {
namespace {
using namespace object_detail;

ObjectReply Invalid(std::string key, std::string message) {
  auto result = Error(400, "invalid_request", std::move(message));
  result.value = Object({{"param", String(std::move(key))}});
  return result;
}

ObjectReply Invocation(const Json& data, const std::string& id) {
  const auto* row = data.Find("resource");
  ChatPlanError error;
  if (!row || row->GetString("id") != id ||
      !ValidateAutomation(*row, AutomationShape::kInvocation, &error))
    return Error(503, "automation_record_invalid", "Invalid saved invocation");
  ObjectReply result;
  result.value = *row;
  return result;
}
}  // namespace

ObjectReply List(ObjectStore& objects, const StateAccess& access,
                 const AutomationRoute& route, std::string_view raw) {
  const bool invocations = route.kind != AutomationRouteKind::kCollection;
  ObjectRequest req;
  req.scope = access.scope;
  req.kind = ObjectKind::kAutomation;
  auto execute = [&] {
    return objects.Execute(req, access.deadline, access.cancelled);
  };
  if (invocations) {
    req.id = route.automation;
    auto parent = execute();
    if (!parent.ok()) return parent;
    req.kind = ObjectKind::kAutomationInvocation;
    if (route.kind == AutomationRouteKind::kInvocation) {
      if (!raw.empty()) return Invalid("query", "Unexpected query");
      req.id = route.invocation;
      auto row = execute();
      if (!row.ok()) return row;
      auto item = Invocation(Field(row.value, "data"), req.id);
      if (!item.ok()) return item;
      if (item.value.GetString("automation_id") != route.automation)
        return Error(404, "object_not_found", "Invocation not found");
      return item;
    }
    req.id.clear();
  }
  auto query = ParseApplicationQuery(raw);
  if (!query.ok()) return query;
  auto options = Object();
  std::string session_id;
  for (const auto& [key, value] : query.value.object) {
    if (key == "after" || key == "order" || key == "limit") {
      Set(&options, key, value);
    } else if (key == "session_id" && !invocations && value.IsString() &&
               value.str.starts_with("agent_session_") && ValidId(value.str)) {
      session_id = value.str;
    } else {
      return Invalid(key, "Invalid automation list option");
    }
  }
  ItemViewOptions page;
  ChatPlanError error;
  auto valid = ParseItemViewOptions(options, &page, &error, true);
  if (!valid) return Invalid(error.param, valid.message());
  const auto matches = [&](const Json& resource) {
    if (invocations)
      return resource.GetString("automation_id") == route.automation;
    return session_id.empty() ||
           resource.Find("target")->GetString("session_id") == session_id;
  };
  const auto project = [&](const Json& row) {
    const auto id = row.GetString("id");
    const auto* data = row.Find("data");
    if (!data)
      return Error(503, "automation_record_invalid", "Missing stored resource");
    return invocations ? Invocation(*data, id) : PublicAutomation(*data, id);
  };
  if (!page.after.empty()) {
    req.id = page.after;
    auto cursor = execute();
    if (!cursor.ok()) return cursor;
    auto item = project(cursor.value);
    if (!item.ok()) return item;
    if (!matches(item.value))
      return Error(404, "object_not_found", "Cursor not found in this listing");
    req.id.clear();
  }
  req.operation = ObjectOperation::kList;
  req.after = page.after;
  req.descending = page.descending;
  req.limit = std::min(16, page.limit + 1);
  auto items = Array();
  bool more = false;
  for (;;) {
    auto result = execute();
    if (!result.ok()) return result;
    for (const auto& row : Field(result.value, "data").array) {
      auto item = project(row);
      if (!item.ok()) return item;
      if (!matches(item.value)) continue;
      if (items.array.size() == static_cast<size_t>(page.limit)) {
        more = true;
        break;
      }
      items.array.push_back(std::move(item.value));
    }
    if (more || !result.value.GetBool("has_more")) break;
    const auto cursor = result.value.GetString("last_id");
    if (cursor.empty() || cursor == req.after)
      return Error(503, "automation_record_invalid", "Invalid cursor");
    req.after = cursor;
  }
  Json first, last;
  if (!items.array.empty()) {
    first = *items.array.front().Find("id");
    last = *items.array.back().Find("id");
  }
  ObjectReply result;
  result.value = Object({{"object", String("list")},
                         {"data", std::move(items)},
                         {"first_id", std::move(first)},
                         {"last_id", std::move(last)},
                         {"has_more", Boolean(more)}});
  return result;
}
}  // namespace q4t::server::automation_detail
