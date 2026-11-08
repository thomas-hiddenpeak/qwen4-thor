#include "q4t/server/responses_state.h"

#include <algorithm>
#include <unordered_set>

#include "object_protocol.h"

namespace q4t::server {
namespace {
using namespace object_detail;
ObjectReply StateError(std::string message) {
  return Error(503, "response_state_invalid", std::move(message));
}
bool Terminal(const Json& response) {
  const auto status = response.GetString("status");
  return status == "completed" || status == "incomplete" ||
         status == "failed" || status == "cancelled";
}
ObjectRequest Write(ObjectKind kind, const std::string& id,
                    const StateAccess& access, int64_t revision, Json data,
                    bool permanent) {
  ObjectRequest request;
  request.operation = ObjectOperation::kPut;
  request.kind = kind;
  request.id = id;
  request.scope = access.scope;
  request.expected_revision = revision;
  request.data = std::move(data);
  if (permanent) request.ttl_seconds = 0;
  return request;
}
Json SavedResponse(const ResponseStatePlan& plan, const Json& response,
                   const Json& events) {
  return Object({{"response", response},
                 {"history", plan.history},
                 {"own_input", plan.own_input},
                 {"request", plan.request},
                 {"events", events}});
}
ObjectReply Data(const ObjectReply& stored, std::string_view field) {
  if (!stored.ok()) return stored;
  const auto* data = stored.value.Find("data");
  const auto* value = data ? data->Find(std::string(field)) : nullptr;
  if (!value) return StateError("Stored response is missing required state");
  ObjectReply result;
  result.value = *value;
  return result;
}
}  // namespace

ObjectReply ResponsesState::Get(ObjectKind kind, const std::string& id,
                                const StateAccess& access) {
  ObjectRequest request;
  request.operation = ObjectOperation::kGet;
  request.scope = access.scope;
  request.kind = kind;
  request.id = id;
  return objects_.Execute(request, access.deadline, access.cancelled);
}

ObjectReply ResponsesState::Resolve(const ResponsesEnvelope& envelope,
                                    const StateAccess& access,
                                    ResponseStatePlan* plan) {
  *plan = {};
  if (access.scope.empty() || access.scope.size() > 256)
    return StateError("Authenticated state scope is unavailable");
  plan->scope = access.scope;
  plan->id = NewObjectId(ObjectKind::kResponse);
  if (plan->id.empty()) return StateError("Response identity unavailable");
  plan->store = envelope.store;
  plan->request = envelope.request;
  plan->history = Array();
  plan->own_input = Array();
  const auto* input = envelope.request.Find("input");
  if (input && input->IsString()) {
    plan->own_input.array.push_back(Object({{"type", String("message")},
                                            {"role", String("user")},
                                            {"content", *input}}));
  } else if (input && input->IsArray()) {
    plan->own_input = *input;
  } else if (input && !input->IsNull()) {
    return Error(400, "invalid_request", "input requires text or items");
  }
  if (plan->own_input.array.size() > 4096)
    return Error(400, "input_limit_exceeded", "Input exceeds 4096 items");
  if (!envelope.previous_response_id.empty()) {
    auto previous =
        Get(ObjectKind::kResponse, envelope.previous_response_id, access);
    if (!previous.ok()) return previous;
    const auto* data = previous.value.Find("data");
    const auto* history = data ? data->GetArray("history") : nullptr;
    const auto* response = data ? data->Find("response") : nullptr;
    if (!history || !response || !response->GetArray("output") ||
        response->GetString("id") != envelope.previous_response_id)
      return StateError("Previous response has inconsistent history");
    if (!Terminal(*response))
      return Error(409, "response_not_completed",
                   "Previous response is still running");
    plan->history = *history;
    const auto& output = response->GetArray("output")->array;
    plan->history.array.insert(plan->history.array.end(), output.begin(),
                               output.end());
    // Previous request.instructions is deliberately not consulted.
  } else if (!envelope.conversation_id.empty()) {
    auto conversation =
        Get(ObjectKind::kConversation, envelope.conversation_id, access);
    if (!conversation.ok()) return conversation;
    const auto* data = conversation.value.Find("data");
    const auto* items = data ? data->GetArray("items") : nullptr;
    if (!items || !data->Find("resource"))
      return StateError("Conversation has inconsistent state");
    plan->conversation_id = envelope.conversation_id;
    plan->conversation_revision = conversation.value.GetInt("revision");
    plan->conversation = *data;
    plan->history = *items;
  }
  std::unordered_set<std::string> own_ids;
  for (auto& item : plan->own_input.array) {
    if (!item.IsObject())
      return Error(400, "invalid_request", "Input item must be an object");
    if (item.GetString("type") == "item_reference") {
      if (item.object.size() != 2 || !ValidId(item.GetString("id")))
        return Error(400, "invalid_request", "Invalid item reference");
      auto reference =
          Data(Get(ObjectKind::kConversationItem, item.GetString("id"), access),
               "item");
      if (!reference.ok()) return reference;
      item = std::move(reference.value);
    }
    if (!item.Has("type") && item.Has("role"))
      Set(&item, "type", String("message"));
    if (item.GetString("type") == "message") {
      if (const auto* content = item.Find("content");
          content && content->IsString()) {
        auto parts = Array(
            {Object({{"type", String("input_text")}, {"text", *content}})});
        Set(&item, "content", std::move(parts));
      }
      if (!item.Has("status")) Set(&item, "status", String("completed"));
    }
    if (!item.Has("id")) {
      auto id = NewObjectId(ObjectKind::kConversationItem);
      if (id.empty()) return StateError("Input item identity unavailable");
      Set(&item, "id", String(std::move(id)));
    }
    const auto id = item.GetString("id");
    if (!ValidId(id) || !own_ids.insert(id).second)
      return Error(400, "invalid_request", "Invalid or repeated input item ID");
  }
  plan->history.array.insert(plan->history.array.end(),
                             plan->own_input.array.begin(),
                             plan->own_input.array.end());
  if (plan->history.array.size() > 4096)
    return Error(400, "input_limit_exceeded",
                 "Resolved history exceeds 4096 items");
  ObjectReply result;
  result.value = plan->history;
  return result;
}

ObjectReply ResponsesState::Begin(const Json& snapshot,
                                  const StateAccess& access,
                                  ResponseStatePlan* plan) {
  if (plan->scope != access.scope)
    return Error(403, "object_scope_mismatch",
                 "Response state belongs to another client");
  if (snapshot.GetString("id") != plan->id || plan->response_revision != 0 ||
      (snapshot.GetString("status") != "queued" &&
       snapshot.GetString("status") != "in_progress"))
    return StateError("Invalid initial response state");
  if (!plan->store) return {};
  auto written =
      objects_.Execute(Write(ObjectKind::kResponse, plan->id, access, 0,
                             SavedResponse(*plan, snapshot, Array()),
                             !plan->conversation_id.empty()),
                       access.deadline, access.cancelled);
  if (written.ok()) plan->response_revision = written.value.GetInt("revision");
  return written;
}

ObjectReply ResponsesState::Finish(const Json& snapshot, const Json& events,
                                   const StateAccess& access,
                                   ResponseStatePlan* plan) {
  if (plan->scope != access.scope)
    return Error(403, "object_scope_mismatch",
                 "Response state belongs to another client");
  if (plan->finished || snapshot.GetString("id") != plan->id ||
      !Terminal(snapshot) || !snapshot.GetArray("output") ||
      !events.IsArray() || (plan->store && plan->response_revision <= 0))
    return StateError("Invalid terminal response state");
  if (!plan->store && plan->conversation_id.empty()) {
    plan->finished = true;
    return {};
  }
  std::vector<ObjectRequest> mutations;
  const bool permanent = !plan->conversation_id.empty();
  if (plan->store)
    mutations.push_back(
        Write(ObjectKind::kResponse, plan->id, access, plan->response_revision,
              SavedResponse(*plan, snapshot, events), permanent));
  auto items = plan->own_input;
  const auto& output = snapshot.GetArray("output")->array;
  items.array.insert(items.array.end(), output.begin(), output.end());
  const bool publish = snapshot.GetString("status") == "completed" ||
                       snapshot.GetString("status") == "incomplete";
  const bool append = permanent && publish;
  if (!publish) items.array.clear();
  if (!plan->store && !append) {
    plan->finished = true;
    return {};
  }
  if (append) {
    auto conversation = plan->conversation;
    auto* previous = &Field(conversation, "items");
    previous->array.insert(previous->array.end(), items.array.begin(),
                           items.array.end());
    mutations.push_back(Write(ObjectKind::kConversation, plan->conversation_id,
                              access, plan->conversation_revision,
                              std::move(conversation), true));
  }
  std::unordered_set<std::string> seen;
  for (const auto& item : items.array) {
    const auto id = item.GetString("id");
    if (!ValidId(id) || !seen.insert(id).second)
      return StateError("Generated item identity invalid or repeated");
    auto existing = Get(ObjectKind::kConversationItem, id, access);
    int64_t revision = 0;
    bool keep = permanent;
    if (existing.ok()) {
      auto original = Data(existing, "item");
      if (!original.ok()) return original;
      if (OutputJson(original.value) != OutputJson(item))
        return Error(409, "item_identity_conflict",
                     "Existing item has different content");
      revision = existing.value.GetInt("revision");
      keep = keep || existing.value.GetInt("expires_at", -1) == 0;
    } else if (existing.http_status != 404) {
      return existing;
    }
    mutations.push_back(Write(ObjectKind::kConversationItem, id, access,
                              revision, Object({{"item", item}}), keep));
  }
  auto written =
      objects_.ExecuteBatch(mutations, access.deadline, access.cancelled);
  if (written.ok()) {
    if (plan->store) ++plan->response_revision;
    if (append) ++plan->conversation_revision;
    plan->finished = true;
  }
  return written;
}

ObjectReply ResponsesState::Retrieve(std::string id,
                                     const StateAccess& access) {
  return Data(Get(ObjectKind::kResponse, id, access), "response");
}
ObjectReply ResponsesState::Erase(std::string id, const StateAccess& access) {
  auto existing = Get(ObjectKind::kResponse, id, access);
  auto response = Data(existing, "response");
  if (!response.ok()) return response;
  if (!Terminal(response.value))
    return Error(409, "response_not_completed",
                 "Cancel an active response before deletion");
  ObjectRequest request;
  request.operation = ObjectOperation::kErase;
  request.kind = ObjectKind::kResponse;
  request.scope = access.scope;
  request.id = std::move(id);
  request.expected_revision = existing.value.GetInt("revision");
  return objects_.Execute(request, access.deadline, access.cancelled);
}
}  // namespace q4t::server
