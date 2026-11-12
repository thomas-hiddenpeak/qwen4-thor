#include "q4t/server/vector_store_api.h"

#include "object_protocol.h"
#include "q4t/server/application_query.h"
#include "q4t/server/application_routes.h"
#include "q4t/server/input_contract.h"
#include "q4t/server/request_json.h"

namespace q4t::server {
namespace {
using namespace object_detail;
ObjectReply Invalid(std::string message, std::string param) {
  auto reply = Error(400, "invalid_request", std::move(message));
  reply.value = Object({{"param", String(std::move(param))}});
  return reply;
}
ObjectReply ListStores(VectorStores& stores, const BinaryAccess& access,
                       const Json& query) {
  std::string param;
  const auto fields = InputFields(query, {"after", "before", "limit", "order"},
                                  &param, "query");
  if (!fields) return Invalid(fields.message(), param);
  ObjectRequest page;
  if (const auto* limit = query.Find("limit")) {
    if (!IsInputInteger(*limit) || limit->AsInt() < 1 || limit->AsInt() > 100)
      return Invalid("limit must be 1..100", "limit");
    page.limit = limit->AsInt();
  }
  for (const auto* key : {"after", "before"}) {
    if (const auto* value = query.Find(key)) {
      if (!value->IsString() || !ValidApplicationId(value->str))
        return Invalid("Invalid vector store cursor", key);
      (std::string_view(key) == "after" ? page.after : page.before) =
          value->str;
    }
  }
  if (!page.after.empty() && !page.before.empty())
    return Invalid("Use one cursor", "query");
  if (const auto* order = query.Find("order")) {
    if (!order->IsString() || (order->str != "asc" && order->str != "desc"))
      return Invalid("order must be asc or desc", "order");
    page.descending = order->str == "desc";
  }
  return stores.List(access, page);
}
}  // namespace

ObjectReply HandleVectorStoreRequest(
    ObjectStore& objects, ClientAdmission& clients, bool ingestion_configured,
    std::string_view method, std::string_view target, const std::string& body,
    const ClientIdentity& client, const BinaryAccess& access) {
  using Kind = VectorStoreRouteKind;
  const auto route = ParseVectorStoreRoute(target);
  if (route.kind == Kind::kInvalid)
    return Error(404, "route_not_found", "Vector store route not found");
  if (!VectorStoreMethodAllowed(method, target)) {
    auto reply = Error(405, "method_not_allowed", "Method not allowed");
    reply.value =
        Object({{"param", String("method")},
                {"allow",
                 String(std::string(VectorStoreAllowedMethods(route.kind)))}});
    return reply;
  }
  if (!clients.OwnsIdentity(client))
    return Error(503, "object_scope_unavailable",
                 "Authenticated application state unavailable");
  const auto scope = client.Scoped() ? "client:" + std::string(client.Id())
                                     : "local:anonymous";
  if (access.scope != scope)
    return Error(403, "object_scope_mismatch", "Vector store client changed");
  const auto split = target.find('?');
  auto query = ParseApplicationQuery(
      split == target.npos ? "" : target.substr(split + 1));
  if (!query.ok()) return query;
  const bool listing = method == "GET" && (route.kind == Kind::kStores ||
                                           route.kind == Kind::kFiles ||
                                           route.kind == Kind::kBatchFiles);
  if (!listing && !query.value.object.empty())
    return Invalid("Query not allowed here", "query");
  if (method != "POST" && !body.empty())
    return Invalid("Body not allowed here", "body");
  Json request = Object();
  if (method == "POST" && !(route.kind == Kind::kBatchCancel && body.empty())) {
    const auto parsed = io::ParseJson(body, &request, true, kRequestJsonLimits);
    if (!parsed) return Invalid(parsed.message(), "body");
    if (!request.IsObject()) return Invalid("Expected an object", "body");
  }
  if (route.kind == Kind::kBatchCancel) {
    std::string param;
    const auto checked = InputFields(request, {}, &param);
    if (!checked) return Invalid(checked.message(), param);
  }
  // Keep observation/cancel/delete usable when the model worker or business
  // quota is unavailable. A durable queued job may wait for worker recovery;
  // transient worker health is not permission to lose an accepted operation.
  if (!ingestion_configured && method == "POST" &&
      (route.kind == Kind::kFiles || route.kind == Kind::kBatches ||
       (route.kind == Kind::kStores && request.GetArray("file_ids") &&
        !request.GetArray("file_ids")->array.empty())))
    return Error(503, "vector_ingestion_unavailable",
                 "Native embedding ingestion is not configured");
  ClientRateLease rate;
  if (!IsVectorStoreObservation(method, target) &&
      !IsVectorStoreCancellation(method, target)) {
    const auto admitted = rate.Reserve(clients, client, 0, 0);
    if (admitted != ClientRateResult::kAllowed) {
      const bool frozen = admitted == ClientRateResult::kFrozen;
      return Error(frozen || admitted == ClientRateResult::kInvalid ? 503 : 429,
                   frozen                               ? "service_draining"
                   : admitted == ClientRateResult::kRpm ? "client_rpm_exhausted"
                   : admitted == ClientRateResult::kTpm ? "client_tpm_exhausted"
                                                        : "client_rate_invalid",
                   "Vector store request could not acquire client quota");
    }
    rate.Start();
  }
  VectorStores stores(objects);
  const std::string id(route.store), child(route.child);
  switch (route.kind) {
    case Kind::kStores:
      return method == "GET" ? ListStores(stores, access, query.value)
                             : stores.Create(access, request);
    case Kind::kStore:
      return method == "GET"      ? stores.Retrieve(access, id)
             : method == "DELETE" ? stores.Erase(access, id)
                                  : stores.Update(access, id, request);
    case Kind::kFiles:
      return method == "GET" ? stores.ListFiles(access, id, query.value)
                             : stores.Attach(access, id, request);
    case Kind::kFile:
      return method == "GET" ? stores.RetrieveFile(access, id, child)
             : method == "DELETE"
                 ? stores.Detach(access, id, child)
                 : stores.UpdateFile(access, id, child, request);
    case Kind::kContent:
      return stores.FileContent(access, id, child);
    case Kind::kBatches:
      return stores.CreateBatch(access, id, request);
    case Kind::kBatch:
      return stores.RetrieveBatch(access, id, child);
    case Kind::kBatchFiles:
      return stores.ListBatchFiles(access, id, child, query.value);
    case Kind::kBatchCancel:
      return stores.CancelBatch(access, id, child);
    case Kind::kInvalid:
      break;
  }
  return Error(404, "route_not_found", "Vector store route not found");
}
}  // namespace q4t::server
