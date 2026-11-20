#include "q4t/server/realtime_mcp_catalog.h"

#include "object_protocol.h"
#include "q4t/server/file_expansion.h"
#include "q4t/server/input_contract.h"
#include "q4t/server/mcp_policy.h"
#include "q4t/server/realtime_conversation.h"
#include "q4t/server/realtime_shape.h"
#include "q4t/server/responses_item_shape.h"

namespace q4t::server {
namespace {
using namespace object_detail;
bool Accepted(const ObjectReply& reply) {
  return reply.ok() && !reply.commit_unknown && reply.http_status >= 200 &&
         reply.http_status < 300;
}
ObjectReply Active(const StateAccess& access) {
  if (access.cancelled && access.cancelled())
    return Error(499, "request_cancelled", "MCP catalog import cancelled");
  if (std::chrono::steady_clock::now() >= access.deadline)
    return Error(408, "request_timeout", "MCP catalog deadline expired");
  return {};
}
}  // namespace

Status ProjectRealtimeMcpCatalog(const Json& catalog, Json* item, bool* loaded,
                                 ChatPlanError* error) {
  if (!item || !loaded || !error)
    return Status::Fail("Missing Realtime MCP catalog output");
  *error = {};
  size_t bytes = 0;
  if (!MeasureRealtimeStorage(catalog, 4ULL << 20, &bytes)) {
    *error = {"realtime_mcp_catalog_capacity", "mcp_list_tools", 413};
    return Status::Fail("Realtime MCP catalog storage exceeded");
  }
  auto status = ValidateResponsesItemShape(catalog, ResponsesItemShape::kOutput,
                                           error, "mcp_list_tools");
  if (!status) return status;
  const auto* tools = catalog.GetArray("tools");
  const auto* failure = catalog.Find("error");
  const bool success = !failure || failure->IsNull();
  if (catalog.GetString("type") != "mcp_list_tools" || !tools ||
      !catalog.GetString("id").starts_with("mcpl_") ||
      !ValidRequestId(catalog.GetString("id")) ||
      (!success && !tools->array.empty())) {
    *error = {"invalid_mcp_catalog", "mcp_list_tools", 400};
    return Status::Fail("Invalid native MCP catalog identity or outcome");
  }
  auto projected = catalog;
  std::erase_if(projected.object,
                [](const auto& field) { return field.first == "error"; });
  status = ValidateRealtimeShape(projected, RealtimeShape::kItem, error,
                                 "mcp_list_tools");
  if (!status) return status;
  *item = std::move(projected);
  *loaded = success;
  return {};
}

ObjectReply ImportRealtimeMcpCatalog(ObjectStore& objects,
                                     const ChatPlanConfig& config,
                                     const ResponsesMcpDefinition& definition,
                                     const StateAccess& access,
                                     std::string reserved_id,
                                     RealtimeMcpCatalog* output) {
  if (!output || access.scope.empty() || !reserved_id.starts_with("mcpl_") ||
      !ValidRequestId(reserved_id))
    return Error(500, "realtime_mcp_owner_invalid",
                 "MCP catalog has no valid import owner");
  auto reply = Active(access);
  if (!Accepted(reply)) return reply;
  RealtimeMcpCatalog next;
  ChatPlanError error;
  auto status = PlanResponsesMcpDefinition(definition.declaration,
                                           &next.definition, &error);
  if (!status) return Error(error.http_status, error.code, status.message());
  // Bind only the caller's already validated private declaration. Deployment
  // resolution is fresh; a stored URL or tunnel binding cannot bypass policy.
  BindResponsesMcpCredentials(&next.definition, {definition});
  if (next.definition.target_kind != definition.target_kind ||
      next.definition.target_id != definition.target_id ||
      next.definition.credentials.authorization !=
          definition.credentials.authorization ||
      next.definition.credentials.headers != definition.credentials.headers)
    return Error(500, "realtime_mcp_owner_invalid",
                 "MCP credentials do not belong to the declared target");
  reply = ResolveMcpDeploymentTarget(config.mcp_policy.get(), access.scope,
                                     &next.definition);
  if (!Accepted(reply)) return reply;
  const auto fetch = config.mcp_policy    ? config.mcp_policy->fetch
                     : config.file_inputs ? config.file_inputs->fetch
                                          : FileFetchOptions{};
  McpTransportReceipt receipt;
  McpSession session;
  reply = InvokeMcp({access.scope, access.deadline, access.cancelled},
                    next.definition.credentials, fetch, "tools/list", Object(),
                    {}, &receipt, {}, &session, config.mcp_tunnels);
  if (reply.commit_unknown || reply.http_status == 408 ||
      reply.http_status == 499)
    return reply;
  const auto* tools = receipt.result.GetArray("tools");
  bool success = Accepted(reply) && receipt.error.IsNull() && tools;
  // Retain useful classification in authenticated diagnostics without copying
  // a remote error message that could echo request credentials.
  std::string failure;
  if (!success) {
    const auto code = !Accepted(reply)          ? reply.ClientCode()
                      : !receipt.error.IsNull() ? "mcp_protocol_error"
                                                : "mcp_catalog_invalid";
    failure = std::string(code) + ": MCP tools/list failed";
  }
  if (success) {
    Json projected, authority;
    status = ProjectResponsesMcpCatalog(next.definition, *tools, &projected,
                                        &authority, &error);
    if (!status) {
      success = false;
      failure =
          "mcp_catalog_invalid: MCP tools/list returned an invalid catalog";
    }
  }
  // Cancellation after remote I/O still cannot publish a successful import.
  auto active = Active(access);
  if (!Accepted(active)) return active;
  ResponsesMcpState state(objects, config);
  auto saved =
      state.Catalog(next.definition, success ? *tools : Array(), access, 3600,
                    std::move(reserved_id), std::move(failure),
                    session.state.Has("protocol") ? &session.state : nullptr);
  if (!Accepted(saved)) return saved;
  next.authority = std::move(saved.value);
  status = ProjectRealtimeMcpCatalog(next.authority, &next.item, &next.loaded,
                                     &error);
  if (!status)
    return Error(500, "realtime_mcp_projection_failed",
                 "Owned MCP catalog could not be projected");
  ObjectReply result;
  result.value = next.item;
  *output = std::move(next);
  return result;
}
}  // namespace q4t::server
