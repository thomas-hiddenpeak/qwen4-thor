#include "assistant_runs_internal.h"

#include <algorithm>
#include <ctime>

#include "object_protocol.h"
#include "q4t/server/request_json.h"
#include "q4t/server/vector_stores.h"

namespace q4t::server {
namespace assistant_detail {
using namespace object_detail;
using Kind = AssistantThreadRouteKind;

ObjectReply Success(Json value) {
  ObjectReply result;
  result.value = std::move(value);
  return result;
}

ObjectReply Corrupt() {
  return Error(503, "thread_record_invalid", "Invalid stored thread");
}

Json Deleted(const std::string& id, const char* type) {
  return Object({{"id", String(id)},
                 {"object", String(type)},
                 {"deleted", Boolean(true)}});
}

ObjectReply ReadThread(const Json& record, Json* data) {
  const auto* value = record.Find("data");
  const auto* thread = value ? value->Find("thread") : nullptr;
  const auto* messages = value ? value->GetArray("messages") : nullptr;
  ChatPlanError error;
  if (!thread || !messages ||
      record.GetString("id") != thread->GetString("id") ||
      !ValidateAssistant(*thread, AssistantShape::kThreadResource, &error))
    return Corrupt();
  for (const auto& message : messages->array) {
    if (message.GetString("thread_id") != thread->GetString("id") ||
        !ValidateAssistant(message, AssistantShape::kMessageResource, &error))
      return Corrupt();
  }
  *data = *value;
  return {};
}

ObjectReply LoadThreadDraft(ObjectStore& objects, const StateAccess& access,
                            std::string_view id, ObjectRequest* draft) {
  auto& write = *draft;
  const bool creating = id.empty();
  write.kind = ObjectKind::kThread;
  write.scope = access.scope;
  write.id = creating ? NewObjectId(write.kind) : std::string(id);
  if (write.id.empty())
    return Error(503, "object_id_unavailable", "No thread ID");
  write.ttl_seconds = 0;
  if (creating) {
    auto thread = Object({{"id", String(write.id)},
                          {"object", String("thread")},
                          {"created_at", Number(std::time(nullptr))},
                          {"tool_resources", Object()},
                          {"metadata", Object()}});
    write.data = Object({{"thread", std::move(thread)}, {"messages", Array()}});
  } else {
    auto record = objects.Execute(write, access.deadline, access.cancelled);
    if (!record.ok()) return record;
    auto valid = ReadThread(record.value, &write.data);
    if (!valid.ok()) return valid;
    write.expected_revision = record.value.GetInt("revision");
  }
  return {};
}

ObjectReply DeleteThread(ObjectStore& objects, ObjectRequest thread,
                         const StateAccess& access) {
  std::vector<ObjectRequest> deletes;
  if (const auto* runs = thread.data.GetArray("runs")) {
    for (const auto& entry : runs->array) {
      auto* ids = entry.GetArray("response_ids");
      if (!ids) return Corrupt();
      for (const auto& id : ids->array) {
        ObjectRequest resp;
        resp.scope = access.scope;
        resp.kind = ObjectKind::kResponse;
        resp.id = id.str;
        auto existing = objects.Execute(resp,access.deadline,access.cancelled);
        if (!existing.ok()) return existing;
        const auto* data = existing.value.Find("data");
        if (!data || data->GetString("assistant_thread_id") != thread.id ||
            data->GetString("assistant_run_id") != entry.Find("run")->GetString("id"))
          return Corrupt();
        resp.operation = ObjectOperation::kErase;
        resp.expected_revision = existing.value.GetInt("revision");
        deletes.push_back(std::move(resp));
      }
    }
  }
  thread.operation = ObjectOperation::kErase;
  thread.data = {};
  deletes.push_back(std::move(thread));
  return objects.ExecuteBatch(deletes,access.deadline,access.cancelled);
}

Json& ObjectField(Json* object, const char* key) {
  auto& field = Field(*object, key);
  if (field.IsNull()) field = Object();
  return field;
}

void AppendIds(Json* object, const char* key,
               const std::vector<std::string>& incoming) {
  auto& ids = Field(*object, key);
  if (ids.IsNull()) ids = Array();
  for (const auto& id : incoming) {
    if (std::ranges::none_of(
            ids.array, [&](const Json& value) { return value.str == id; }))
      ids.array.push_back(String(id));
  }
}

// Prepare tool resources before publishing. If file_search creates or extends a
// store, the thread, its message, the index jobs and membership share one CAS.
ObjectReply SaveThread(ObjectStore& objects, const BinaryAccess& access,
                 bool creating, bool resources_changed, bool ingestion,
                 const Attachments& files, ObjectRequest* write,
                 std::vector<ObjectRequest>* prepared) {
  auto& thread = Field(write->data, "thread");
  auto& resources = Field(thread, "tool_resources");
  Json inline_store;
  std::string new_id, existing_id;
  if (resources_changed && !resources.IsNull()) {
    auto bound = BindResources(objects, access, &resources, creating, ingestion,
                               &inline_store, &new_id);
    if (!bound.ok()) return bound;
  }
  if (!files.code.empty() || !files.search.empty()) {
    if (resources.IsNull()) resources = Object();
    if (!files.code.empty()) {
      auto& code = ObjectField(&resources, "code_interpreter");
      AppendIds(&code, "file_ids", files.code);
      if (Field(code, "file_ids").array.size() > 20)
        return Invalid("At most 20 code interpreter files", "attachments");
    }
    if (!files.search.empty()) {
      if (!ingestion)
        return Error(503, "vector_ingestion_unavailable",
                     "Native embedding ingestion is not configured");
      auto& search = ObjectField(&resources, "file_search");
      const auto* ids = search.GetArray("vector_store_ids");
      if (new_id.empty() && ids && !ids->array.empty()) {
        existing_id = ids->array.front().str;
      } else {
        if (new_id.empty()) {
          new_id = NewObjectId(ObjectKind::kVectorStore);
          if (new_id.empty())
            return Error(503, "object_id_unavailable", "No vector store ID");
          inline_store = Object(
              {{"expires_after", Object({{"anchor", String("last_active_at")},
                                         {"days", Number(7)}})}});
          Set(&search, "vector_store_ids", Array({String(new_id)}));
        }
        AppendIds(&inline_store, "file_ids", files.search);
      }
    }
  }
  ChatPlanError error;
  auto valid =
      ValidateAssistant(thread, AssistantShape::kThreadResource, &error);
  if (!valid) return Invalid(valid.message(), error.param);
  if (!new_id.empty())
    return VectorStores(objects).Create(access, inline_store, new_id,
                                        {write, 1}, prepared);
  if (!existing_id.empty())
    return VectorStores(objects).AttachForThread(access, existing_id,
                                                 files.search, *write, prepared);
  if (prepared) {
    *prepared = {*write};
    return {};
  }
  return objects.Execute(*write, access.deadline, access.cancelled);
}
}  // namespace assistant_detail
using namespace assistant_detail;

ObjectReply HandleAssistantThreads(ObjectStore& objects,
                                   std::string_view method,
                                   std::string_view target,
                                   const std::string& body,
                                   const StateAccess& access, bool ingestion) {
  const auto route = ParseAssistantThreadRoute(target);
  if (route.kind == Kind::kNone)
    return Error(404, "route_not_found", "Thread route not found");
  if (!AssistantMethodAllowed(method, target)) {
    auto reply = Error(405, "method_not_allowed", "Method not allowed");
    const char* allowed = route.kind == Kind::kCollection ? "POST"
                          : route.kind == Kind::kMessages ? "GET, POST"
                                                          : "GET, POST, DELETE";
    reply.value = Object({{"allow", String(allowed)}});
    return reply;
  }
  const auto split = target.find('?');
  const bool listing = method == "GET" && route.kind == Kind::kMessages;
  if (!listing && split != target.npos)
    return Invalid("Unexpected query", "query");
  const bool creating = route.kind == Kind::kCollection;
  Json input;
  if (method == "POST") {
    if (creating && body.empty()) {
      input = Object();
    } else {
      auto parsed = io::ParseJson(body, &input, true, kSchemaJsonLimits);
      if (!parsed) return Invalid(parsed.message(), "body");
      if (creating && input.IsNull()) input = Object();
    }
    const auto shape =
        creating                        ? AssistantShape::kThreadCreate
        : route.kind == Kind::kThread   ? AssistantShape::kThreadUpdate
        : route.kind == Kind::kMessages ? AssistantShape::kMessageCreate
                                        : AssistantShape::kMessageUpdate;
    ChatPlanError error;
    auto valid = ValidateAssistant(input, shape, &error);
    if (!valid) return Invalid(valid.message(), error.param);
  } else if (!body.empty()) {
    return Invalid("Unexpected body", "body");
  }
  ObjectRequest write;
  auto loaded = LoadThreadDraft(objects, access, route.thread, &write);
  if (!loaded.ok()) return loaded;
  if (!creating) {
    auto observed = ObserveLastRun(objects, access, &write.data, method != "GET");
    if (!observed.ok()) return observed;
  }
  auto& thread = Field(write.data, "thread");
  auto& messages = Field(write.data, "messages");
  const BinaryAccess binary{access.scope, access.deadline, access.cancelled};
  if (listing)
    return ListMessages(messages,
                        split == target.npos ? "" : target.substr(split + 1));
  Json response;
  Attachments files;
  const bool thread_route = creating || route.kind == Kind::kThread;
  if (thread_route) {
    if (method == "GET") return Success(thread);
    if (method == "DELETE") {
      auto result = DeleteThread(objects, write, access);
      if (result.ok()) result.value = Deleted(write.id, "thread.deleted");
      if (result.commit_unknown)
        result.value = Object({{"id", String(write.id)}});
      return result;
    }
    for (const auto* key : {"metadata", "tool_resources"})
      if (const auto* value = input.Find(key)) Set(&thread, key, *value);
    if (const auto* seed = input.GetArray("messages")) {
      for (const auto& item : seed->array) {
        Json row;
        auto made =
            CreateMessage(objects, binary, write.id, item, &files, &row);
        if (!made.ok()) return made;
        messages.array.push_back(std::move(row));
      }
    }
  } else if (route.kind == Kind::kMessages) {
    auto made =
        CreateMessage(objects, binary, write.id, input, &files, &response);
    if (!made.ok()) return made;
    messages.array.push_back(response);
  } else {
    auto it = std::ranges::find_if(messages.array, [&](const auto& msg) {
      return msg.GetString("id") == route.message;
    });
    if (it == messages.array.end())
      return Error(404, "message_not_found",
                   "Message not found in this thread");
    if (method == "GET") return Success(*it);
    if (method == "DELETE") {
      response = Deleted(it->GetString("id"), "thread.message.deleted");
      messages.array.erase(it);
    } else {
      if (const auto* metadata = input.Find("metadata"))
        Set(&*it, "metadata", *metadata);
      response = *it;
    }
  }
  write.operation = ObjectOperation::kPut;
  auto result = SaveThread(objects, binary, creating, input.Has("tool_resources"),
                     ingestion, files, &write);
  if (result.ok()) result.value = thread_route ? thread : response;
  if (result.commit_unknown)
    result.value = Object(
        {{"id", thread_route ? String(write.id) : *response.Find("id")}});
  return result;
}
}  // namespace q4t::server
