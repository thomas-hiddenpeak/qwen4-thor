#include "q4t/server/conversation_api.h"

#include "object_protocol.h"
#include "q4t/server/application_query.h"
#include "q4t/server/input_contract.h"
#include "q4t/server/request_json.h"

namespace q4t::server {
namespace {
using namespace object_detail;
constexpr std::string_view kRoot = "/v1/conversations";
ObjectReply Invalid(std::string message, std::string param) {
  auto result = Error(400, "invalid_request", std::move(message));
  result.value = Object({{"param", String(std::move(param))}});
  return result;
}
ObjectReply Method(std::string allowed) {
  auto result = Error(405, "method_not_allowed", "Method not allowed");
  result.value = Object(
      {{"param", String("method")}, {"allow", String(std::move(allowed))}});
  return result;
}
ObjectReply CheckFields(const Json& body,
                        std::initializer_list<std::string_view> allowed) {
  std::string path;
  auto status = InputFields(body, allowed, &path);
  return status ? ObjectReply() : Invalid(status.message(), path);
}
ObjectReply ResolveInput(ObjectStore& objects, const Json& input,
                         const StateAccess& access) {
  if (!input.IsArray()) return Invalid("items requires an array", "items");
  ResponsesState responses(objects);
  auto result = responses.ResolveItems(input, access, 20);
  if (!result.ok()) {
    auto path = result.value.GetString("param");
    if (path.starts_with("input")) {
      path.replace(0, 5, "items");
      result.value = Object({{"param", String(path)}});
    }
    return result;
  }
  // Compaction is a model operation, not a record transformation. Its A07
  // executor must supply the resulting compaction item before publication.
  for (const auto& item : result.value.array)
    if (item.GetString("type") == "compaction_trigger")
      return Error(503, "compaction_executor_unavailable",
                   "Context compaction executor is not available");
  return result;
}
}  // namespace

ObjectReply HandleConversationRequest(ObjectStore& objects,
                                      std::string_view method,
                                      std::string_view target,
                                      const std::string& body,
                                      const StateAccess& access) {
  const auto separator = target.find('?');
  const auto path = target.substr(0, separator);
  if (!IsConversationTarget(path))
    return Error(404, "route_not_found", "Conversation route not found");
  auto query = ParseApplicationQuery(
      separator == target.npos ? "" : target.substr(separator + 1));
  if (!query.ok()) return query;
  std::string id, item_id;
  bool collection = path == kRoot, item_collection = false;
  if (!collection) {
    auto suffix = path.substr(kRoot.size() + 1);
    const auto slash = suffix.find('/');
    id = suffix.substr(0, slash);
    if (!ValidId(id))
      return Invalid("Invalid conversation identity", "conversation_id");
    if (slash != suffix.npos) {
      suffix.remove_prefix(slash);
      if (suffix == "/items")
        item_collection = true;
      else if (suffix.starts_with("/items/")) {
        item_id = suffix.substr(7);
        if (!ValidId(item_id))
          return Invalid("Invalid item identity", "item_id");
      } else {
        return Error(404, "route_not_found", "Conversation route not found");
      }
    }
  }
  if (collection && method != "POST") return Method("POST");
  if (item_collection && method != "GET" && method != "POST")
    return Method("GET, POST");
  if (!item_id.empty() && method != "GET" && method != "DELETE")
    return Method("GET, DELETE");
  if (!collection && !item_collection && item_id.empty() && method != "GET" &&
      method != "POST" && method != "DELETE")
    return Method("GET, POST, DELETE");
  if (method != "POST" && !body.empty())
    return Invalid("Request body is not allowed on this operation", "body");

  ItemViewOptions view;
  ChatPlanError error;
  if (item_collection || (!item_id.empty() && method == "GET")) {
    const auto status = ParseItemViewOptions(
        query.value, &view, &error, item_collection && method == "GET");
    if (!status) return Invalid(status.message(), error.param);
  } else if (!query.value.object.empty()) {
    return Invalid("Query is not allowed on this operation", "query");
  }
  Json request = Object();
  if (method == "POST" && !body.empty()) {
    auto status = io::ParseJson(body, &request, true, kApplicationJsonLimits);
    if (!status) return Invalid(status.message(), "body");
    if (collection && request.IsNull()) request = Object();
  }
  ConversationState conversations(objects);
  if (collection) {
    auto fields = CheckFields(request, {"metadata", "items"});
    if (!fields.ok()) return fields;
    const auto* metadata = request.Find("metadata");
    auto normalized =
        NormalizeConversationMetadata(metadata ? *metadata : Json());
    if (!normalized.ok()) return normalized;
    const auto* items = request.Find("items");
    auto resolved = ResolveInput(
        objects, !items || items->IsNull() ? Array() : *items, access);
    if (!resolved.ok()) return resolved;
    return conversations.Create(normalized.value, resolved.value, access);
  }
  if (item_collection) {
    if (method == "GET") return conversations.ListItems(id, view, access);
    auto fields = CheckFields(request, {"items"});
    if (!fields.ok()) return fields;
    const auto* items = request.Find("items");
    if (!items) return Invalid("items is required", "items");
    // Check ownership before resolving references from an append request.
    auto existing = conversations.Retrieve(id, access);
    if (!existing.ok()) return existing;
    auto resolved = ResolveInput(objects, *items, access);
    if (!resolved.ok()) return resolved;
    return conversations.Append(id, resolved.value, view, access);
  }
  if (!item_id.empty())
    return method == "GET"
               ? conversations.RetrieveItem(id, item_id, view, access)
               : conversations.EraseItem(id, item_id, access);
  if (method == "GET") return conversations.Retrieve(id, access);
  if (method == "DELETE") return conversations.Erase(id, access);
  auto fields = CheckFields(request, {"metadata"});
  if (!fields.ok()) return fields;
  const auto* metadata = request.Find("metadata");
  if (!metadata) return Invalid("metadata is required", "metadata");
  return conversations.Update(id, *metadata, access);
}
}  // namespace q4t::server
