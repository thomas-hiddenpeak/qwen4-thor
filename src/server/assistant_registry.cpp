#include "q4t/server/assistant_registry.h"

#include <algorithm>
#include <ctime>

#include "assistant_threads_internal.h"
#include "object_protocol.h"
#include "q4t/server/application_query.h"
#include "q4t/server/files.h"
#include "q4t/server/model_catalog.h"
#include "q4t/server/request_json.h"
#include "q4t/server/vector_stores.h"

namespace q4t::server {
namespace assistant_detail {
using namespace object_detail;
ObjectReply Invalid(std::string message, std::string field) {
  auto result = Error(400, "invalid_request", std::move(message));
  result.value = Object({{"param", String(std::move(field))}});
  return result;
}

ObjectReply BindResources(ObjectStore& objects, const BinaryAccess& access,
                          Json* resources, bool creating,
                          bool ingestion_configured, Json* inline_store,
                          std::string* store_id) {
  if (const auto* code = resources->Find("code_interpreter")) {
    if (const auto* files = code->GetArray("file_ids")) {
      if (files->array.size() > 20)
        return Invalid("At most 20 code interpreter files", "tool_resources");
      for (const auto& id : files->array) {
        auto checked = Files(objects).Retrieve(access, id.str);
        if (!checked.ok()) return checked;
      }
    }
  }
  if (!resources->Has("file_search")) return {};
  auto& search = Field(*resources, "file_search");
  const auto* ids = search.GetArray("vector_store_ids");
  const auto* stores = search.GetArray("vector_stores");
  if ((creating && !ids && !stores) || (ids && stores) ||
      (ids && ids->array.size() > 1) || (stores && stores->array.size() > 1))
    return Invalid("Choose at most one attached or inline vector store",
                   "tool_resources.file_search");
  if (ids && !ids->array.empty()) {
    auto existing = VectorStores(objects).Retrieve(access, ids->array[0].str);
    if (!existing.ok()) return existing;
  }
  if (!stores) return {};
  auto bound = Array();
  if (!stores->array.empty()) {
    *inline_store = stores->array.front();
    const auto* files = inline_store->GetArray("file_ids");
    if (files && !files->array.empty() && !ingestion_configured)
      return Error(503, "vector_ingestion_unavailable",
                   "Native embedding ingestion is not configured");
    *store_id = NewObjectId(ObjectKind::kVectorStore);
    if (store_id->empty())
      return Error(503, "object_id_unavailable", "No vector store ID");
    bound.array.push_back(String(*store_id));
  }
  std::erase_if(search.object, [](const auto& field) {
    return field.first == "vector_stores";
  });
  Set(&search, "vector_store_ids", std::move(bound));
  return {};
}
}  // namespace assistant_detail
namespace {
using namespace object_detail;
using namespace assistant_detail;
constexpr std::string_view kRoot = "/v1/assistants";

ObjectReply ReadResource(const Json& record, Json* resource) {
  const auto* data = record.Find("data");
  const auto* row = data ? data->Find("assistant") : nullptr;
  ChatPlanError error;
  if (!row || row->GetString("id") != record.GetString("id") ||
      !ValidateAssistant(*row, AssistantShape::kResource, &error))
    return Error(503, "assistant_record_invalid", "Invalid stored assistant");
  *resource = *row;
  return {};
}

ObjectReply List(ObjectStore& objects, ObjectRequest page, std::string_view raw,
                 const StateAccess& access) {
  auto query = ParseApplicationQuery(raw);
  if (!query.ok()) return query;
  ChatPlanError error;
  auto valid = ValidateAssistant(query.value, AssistantShape::kQuery, &error);
  if (!valid) return Invalid(valid.message(), error.param);
  page.limit = query.value.GetInt("limit", 20);
  page.after = query.value.GetString("after");
  page.before = query.value.GetString("before");
  page.descending = query.value.GetString("order", "desc") == "desc";
  if (page.limit < 1 || page.limit > 100)
    return Invalid("Expected a limit from 1 to 100", "limit");
  if (!page.after.empty() && !page.before.empty())
    return Invalid("Choose one pagination cursor", "before");
  for (const auto* key : {"after", "before"}) {
    if (query.value.Has(key) && !IsAssistantTarget(std::string(kRoot) + "/" +
                                                   query.value.GetString(key)))
      return Invalid("Invalid assistant cursor", key);
  }
  page.operation = ObjectOperation::kList;
  auto result = objects.Execute(page, access.deadline, access.cancelled);
  if (!result.ok()) return result;
  for (auto& record : Field(result.value, "data").array) {
    Json row;
    auto checked = ReadResource(record, &row);
    if (!checked.ok()) return checked;
    record = std::move(row);
  }
  return result;
}

}  // namespace

ObjectReply HandleAssistantRegistry(
    ObjectStore& objects, std::string_view method, std::string_view target,
    const std::string& body, const ChatPlanConfig& config,
    const StateAccess& access, bool ingestion_configured) {
  const auto split = target.find('?');
  const auto path = target.substr(0, split);
  const bool collection = path == kRoot;
  if (!IsAssistantTarget(target))
    return Error(404, "route_not_found", "Assistant route not found");
  if (!AssistantMethodAllowed(method, target)) {
    auto result = Error(405, "method_not_allowed", "Method not allowed");
    result.value = Object(
        {{"allow", String(collection ? "GET, POST" : "GET, POST, DELETE")}});
    return result;
  }
  if (method != "POST" && !body.empty())
    return Invalid("Unexpected body", "body");
  ObjectRequest request;
  request.kind = ObjectKind::kAssistant;
  request.scope = access.scope;
  if (collection && method == "GET")
    return List(objects, request,
                split == target.npos ? "" : target.substr(split + 1), access);
  if (split != target.npos) return Invalid("Unexpected query", "query");
  Json input;
  ChatPlanError error;
  if (method == "POST") {
    auto parsed = io::ParseJson(body, &input, true, kSchemaJsonLimits);
    if (!parsed) return Invalid(parsed.message(), "body");
    auto valid = ValidateAssistant(
        input, collection ? AssistantShape::kCreate : AssistantShape::kUpdate,
        &error);
    if (!valid) return Invalid(valid.message(), error.param);
  }
  const auto execute = [&] {
    return objects.Execute(request, access.deadline, access.cancelled);
  };
  Json row;
  Json effort;
  if (collection) {
    request.id = NewObjectId(request.kind);
    if (request.id.empty())
      return Error(503, "object_id_unavailable", "No assistant ID");
    request.expected_revision = 0;
    row = Object({{"id", String(request.id)},
                  {"object", String("assistant")},
                  {"created_at", Number(std::time(nullptr))},
                  {"name", {}},
                  {"description", {}},
                  {"instructions", {}},
                  {"metadata", Object()},
                  {"tools", Array()},
                  {"tool_resources", Object()},
                  {"response_format", String("auto")},
                  {"temperature", Number(1)},
                  {"top_p", Number(1)}});
  } else {
    request.id = path.substr(kRoot.size() + 1);
    auto stored = execute();
    if (!stored.ok()) return stored;
    auto checked = ReadResource(stored.value, &row);
    if (!checked.ok()) return checked;
    if (method == "GET") {
      stored.value = std::move(row);
      return stored;
    }
    if (const auto* old = Field(stored.value, "data").Find("reasoning_effort"))
      effort = *old;
    request.expected_revision = stored.value.GetInt("revision");
  }
  if (method == "DELETE") {
    request.operation = ObjectOperation::kErase;
    auto result = execute();
    if (result.commit_unknown)
      result.value = Object({{"id", String(request.id)}});
    if (result.ok())
      result.value = Object({{"id", String(request.id)},
                             {"deleted", Boolean(true)},
                             {"object", String("assistant.deleted")}});
    return result;
  }
  for (const auto& [key, value] : input.object) {
    if (key == "reasoning_effort")
      effort = value;
    else
      Set(&row, key, value);
  }
  if (!IsServedModel(row.GetString("model"), config.model,
                     config.model_aliases)) {
    auto result = Error(404, "model_not_found", "Unknown assistant model");
    result.value = Object({{"param", String("model")}});
    return result;
  }
  const BinaryAccess binary{access.scope, access.deadline, access.cancelled};
  Json inline_store;
  std::string store_id;
  if (input.Has("tool_resources")) {
    auto bound = BindResources(objects, binary, &Field(row, "tool_resources"),
                               collection, ingestion_configured, &inline_store,
                               &store_id);
    if (!bound.ok()) return bound;
  }
  auto valid = ValidateAssistant(row, AssistantShape::kResource, &error);
  if (!valid) return Invalid(valid.message(), error.param);
  request.operation = ObjectOperation::kPut;
  request.ttl_seconds = 0;
  request.data =
      Object({{"assistant", row}, {"reasoning_effort", std::move(effort)}});
  auto result = store_id.empty()
                    ? execute()
                    : VectorStores(objects).Create(binary, inline_store,
                                                   store_id, {&request, 1});
  if (result.commit_unknown)
    result.value = Object({{"id", String(request.id)}});
  if (result.ok()) result.value = std::move(row);
  return result;
}
}  // namespace q4t::server
