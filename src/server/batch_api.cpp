#include "q4t/server/batch_api.h"

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
}  // namespace

ObjectReply HandleBatchRequest(ObjectStore& objects, BatchJobs* jobs,
                               std::string_view method, std::string_view target,
                               const std::string& body,
                               const ClientIdentity& client,
                               const BinaryAccess& access) {
  constexpr std::string_view root = "/v1/batches";
  if (!IsBatchTarget(target) || target.find('#') != target.npos)
    return Error(404, "route_not_found", "Batch route not found");
  const auto split = target.find('?');
  auto path = target.substr(0, split);
  const bool collection = path == root;
  bool cancel = false;
  std::string id;
  if (!collection) {
    path.remove_prefix(root.size() + 1);
    const auto slash = path.find('/');
    id = path.substr(0, slash);
    if (!ValidApplicationId(id) ||
        (slash != path.npos && path.substr(slash) != "/cancel"))
      return Error(404, "route_not_found", "Batch route not found");
    cancel = slash != path.npos;
  }
  if (!BatchMethodAllowed(method, target)) {
    auto reply = Error(405, "method_not_allowed", "Method not allowed");
    reply.value = Object({{"param", String("method")},
                          {"allow", String(collection ? "GET, POST"
                                           : cancel   ? "POST"
                                                      : "GET")}});
    return reply;
  }
  if (!client.Allowed())
    return Error(503, "object_scope_unavailable",
                 "Authenticated application state unavailable");
  const auto scope = client.Scoped() ? "client:" + std::string(client.Id())
                                     : "local:anonymous";
  if (access.scope != scope)
    return Error(403, "object_scope_mismatch", "Batch client changed");
  auto query = ParseApplicationQuery(
      split == target.npos ? "" : target.substr(split + 1));
  if (!query.ok()) return query;
  if (!(collection && method == "GET") && !query.value.object.empty())
    return Invalid("Query not allowed here", "query");
  if (method == "GET" && !body.empty())
    return Invalid("Body not allowed here", "body");
  BatchState batches(objects);
  if (method == "GET")
    return collection ? batches.List(access, query.value)
                      : batches.Retrieve(access, id);
  Json request = Object();
  if (!(cancel && body.empty())) {
    const auto parsed = io::ParseJson(body, &request, true, kRequestJsonLimits);
    if (!parsed) return Invalid(parsed.message(), "body");
  }
  if (cancel) {
    std::string param;
    const auto checked = InputFields(request, {}, &param);
    if (!checked) return Invalid(checked.message(), param);
    // Cancellation remains available during auxiliary executor degradation.
    return jobs ? jobs->Cancel(access, id) : batches.Cancel(access, id);
  }
  BatchCreate options;
  const auto parsed = ParseBatchCreate(request, &options);
  if (!parsed.ok()) return parsed;
  if (!jobs)
    return Error(503, "batch_executor_unavailable",
                 "Batch executor unavailable");
  return jobs->Submit(access, options, client);
}
}  // namespace q4t::server
