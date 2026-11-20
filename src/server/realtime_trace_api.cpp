#include "q4t/server/realtime_trace_api.h"

#include "object_protocol.h"
#include "q4t/server/application_query.h"
#include "q4t/server/input_contract.h"

namespace q4t::server {
namespace {
using namespace object_detail;
ObjectReply Invalid(std::string message, std::string param) {
  auto result = Error(400, "invalid_request", std::move(message));
  result.value = Object({{"param", String(std::move(param))}});
  return result;
}
}  // namespace
Json RealtimeTraceDiagnostics(const RealtimeTraceStore* store) {
  const auto health = store ? store->Health() : RealtimeTraceStoreHealth{};
  const auto count = [&](uint64_t value) {
    return health.snapshot_available ? Number(value) : Json();
  };
  return Object({{"configured", Boolean(store != nullptr)},
                 {"readiness_dependency", Boolean(false)},
                 {"model_restart_dependency", Boolean(false)},
                 {"snapshot_available", Boolean(health.snapshot_available)},
                 {"exporter_running",
                  health.snapshot_available ? Boolean(health.running) : Json()},
                 {"pending", count(health.pending)},
                 {"queued_bytes", count(health.queued_bytes)},
                 {"producers", count(health.producers)},
                 {"accepted_snapshots", Number(health.accepted)},
                 {"confirmed_writes", Number(health.persisted)},
                 {"dropped_snapshots", Number(health.dropped)},
                 {"failures", Number(health.failures)},
                 {"controller_failures", count(health.controller_failures)},
                 {"controller_recoveries", count(health.controller_recoveries)},
                 {"durability", String("confirmed_snapshots_only")}});
}
ObjectReply HandleRealtimeTraceRequest(RealtimeTraceStore& store,
                                       std::string_view method,
                                       std::string_view target,
                                       const std::string& body,
                                       const StateAccess& access) {
  constexpr std::string_view root = "/v1/realtime/traces";
  const auto split = target.find('?');
  const auto path = target.substr(0, split);
  if (!IsRealtimeTraceTarget(target) || target.find('#') != target.npos)
    return Error(404, "route_not_found", "Trace route not found");
  const bool collection = path == root;
  const auto id =
      collection ? std::string_view{} : path.substr(root.size() + 1);
  if (!collection && !ValidId(id))
    return Error(404, "route_not_found", "Trace route not found");
  if (method != "GET" && (collection || method != "DELETE")) {
    auto result = Error(405, "method_not_allowed", "Method not allowed");
    result.value =
        Object({{"allow", String(collection ? "GET" : "GET, DELETE")}});
    return result;
  }
  if (!body.empty())
    return Invalid("Trace reads/deletes do not accept a body", "body");
  auto query = ParseApplicationQuery(
      split == target.npos ? "" : target.substr(split + 1));
  if (!query.ok()) return query;
  if (!collection && !query.value.object.empty())
    return Invalid("Query not allowed on a trace ID", "query");
  std::string field;
  const auto fields = InputFields(
      query.value, {"limit", "after", "before", "order", "session_id"}, &field);
  if (!fields) return Invalid(fields.message(), field);
  ObjectRequest request;
  request.kind = ObjectKind::kTrace;
  request.scope = access.scope;
  request.id = id;
  request.operation = method == "DELETE" ? ObjectOperation::kErase
                      : collection       ? ObjectOperation::kList
                                         : ObjectOperation::kGet;
  if (const auto* session = query.value.Find("session_id")) {
    if (!session->IsString() || !ValidRequestId(session->str) ||
        query.value.object.size() != 1)
      return Invalid("session_id must be the only query parameter",
                     "session_id");
    request.operation = ObjectOperation::kGet;
    request.id = "trace_" + Digest(session->str);
    auto result = store.Execute(request, access.deadline, access.cancelled);
    if (!result.ok() && result.http_status != 404) return result;
    auto rows = Array();
    if (result.ok()) rows.array.push_back(std::move(result.value));
    ObjectReply reply;
    reply.value = Object({{"object", String("list")},
                          {"data", std::move(rows)},
                          {"has_more", Boolean(false)}});
    return reply;
  }
  if (const auto* limit = query.value.Find("limit")) {
    if (!IsInputInteger(*limit) || limit->AsInt() < 1 || limit->AsInt() > 100)
      return Invalid("Trace limit must be between 1 and 100", "limit");
    request.limit = limit->AsInt();
  }
  request.after = query.value.GetString("after");
  request.before = query.value.GetString("before");
  const auto order = query.value.GetString("order", "desc");
  if (order != "asc" && order != "desc")
    return Invalid("Invalid order", "order");
  request.descending = order == "desc";
  return store.Execute(std::move(request), access.deadline, access.cancelled);
}
}  // namespace q4t::server
