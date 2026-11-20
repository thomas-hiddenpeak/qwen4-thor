#include "q4t/server/realtime_mcp_execution.h"

#include "object_protocol.h"
#include "q4t/server/chat_response_parser.h"
#include "q4t/server/file_expansion.h"
#include "q4t/server/input_contract.h"
#include "q4t/server/input_snapshot_bindings.h"
#include "q4t/server/mcp_policy.h"
#include "q4t/server/realtime_conversation.h"
#include "q4t/server/realtime_mcp_item.h"
#include "q4t/server/realtime_shape.h"
#include "q4t/server/responses_owner.h"
#include "q4t/server/responses_program.h"

namespace q4t::server {
namespace {
using namespace object_detail;
bool Accepted(const ObjectReply& reply) {
  return reply.ok() && !reply.commit_unknown && reply.http_status >= 200 &&
         reply.http_status < 300;
}
ObjectReply Invalid(std::string message) {
  return Error(400, "realtime_mcp_call_invalid", std::move(message));
}
ObjectReply Active(const std::string& response_id, const StateAccess& access) {
  if (access.scope.empty() || !response_id.starts_with("resp_") ||
      !ValidRequestId(response_id))
    return Invalid("MCP operation has no valid response owner");
  if (access.cancelled && access.cancelled())
    return Error(499, "request_cancelled", "MCP operation cancelled");
  if (std::chrono::steady_clock::now() >= access.deadline)
    return Error(408, "request_timeout", "MCP operation deadline expired");
  return {};
}
bool Same(const Json& a, const Json& b) {
  std::string left, right;
  return InputValueIdentity(a, &left) && InputValueIdentity(b, &right) &&
         left == right;
}
bool SameCall(const Json& a, const Json& b) {
  for (const auto* field : {"id", "name", "server_label", "arguments"})
    if (a.GetString(field) != b.GetString(field)) return false;
  const auto* left = a.Find("approval_request_id");
  const auto* right = b.Find("approval_request_id");
  return Same(left ? *left : Json(), right ? *right : Json());
}
ObjectReply Pending(const Json& item) {
  size_t bytes = 0;
  if (!MeasureRealtimeStorage(item, 4ULL << 20, &bytes))
    return Error(413, "realtime_mcp_call_capacity",
                 "MCP item exceeds capacity");
  ChatPlanError error;
  auto status = ValidateRealtimeShape(item, RealtimeShape::kItem, &error);
  if (!status) return Error(error.http_status, error.code, status.message());
  if (item.GetString("type") != "mcp_call" ||
      !item.GetString("id").starts_with("mcp_") ||
      !ValidRequestId(item.GetString("id")))
    return Invalid("Expected a generated MCP call");
  for (const auto* field : {"output", "error"})
    if (const auto* value = item.Find(field); value && !value->IsNull())
      return Invalid("Generated MCP call already contains a result");
  return {};
}
ObjectReply Resolve(const ResponsesMcpDefinition& requested,
                    const ChatPlanConfig& config, const StateAccess& access,
                    ResponsesMcpDefinition* resolved) {
  ChatPlanError error;
  auto status =
      PlanResponsesMcpDefinition(requested.declaration, resolved, &error);
  if (!status) return Error(error.http_status, error.code, status.message());
  BindResponsesMcpCredentials(resolved, {requested});
  if (resolved->target_kind != requested.target_kind ||
      resolved->target_id != requested.target_id ||
      resolved->credentials.authorization !=
          requested.credentials.authorization ||
      resolved->credentials.headers != requested.credentials.headers)
    return Invalid("MCP credentials differ from their declared target");
  return ResolveMcpDeploymentTarget(config.mcp_policy.get(), access.scope,
                                    resolved);
}
}  // namespace

ObjectReply ProposeRealtimeMcpCall(ObjectStore& objects,
                                   const ChatPlanConfig& config,
                                   const RealtimeMcpCatalog& catalog,
                                   const Json& generated,
                                   const std::string& response_id,
                                   const StateAccess& access) {
  auto reply = Active(response_id, access);
  if (!Accepted(reply)) return reply;
  reply = Pending(generated);
  if (!Accepted(reply)) return reply;
  if (const auto* approval = generated.Find("approval_request_id");
      approval && !approval->IsNull())
    return Invalid("New generated call cannot supply approval authority");
  Json projected;
  bool loaded = false;
  ChatPlanError error;
  const auto projected_status =
      ProjectRealtimeMcpCatalog(catalog.authority, &projected, &loaded, &error);
  if (!projected_status || !loaded || !catalog.loaded ||
      !Same(projected, catalog.item))
    return Invalid("MCP generation has no successfully imported catalog");
  ResponsesMcpDefinition definition;
  reply = Resolve(catalog.definition, config, access, &definition);
  if (!Accepted(reply)) return reply;
  if (generated.GetString("server_label") !=
      definition.declaration.GetString("server_label"))
    return Invalid("Generated MCP call changed its server label");
  Json arguments;
  if (generated.GetString("arguments").size() >
          ChatResponseParser::kMaxToolBytes ||
      !Parse(generated.GetString("arguments"), &arguments) ||
      !arguments.IsObject())
    return Invalid("Generated MCP arguments are not a complete JSON object");
  ResponsesMcpState state(objects, config, response_id);
  return state.Propose(catalog.authority, definition,
                       generated.GetString("name"), std::move(arguments),
                       "direct", access, generated.GetString("id"),
                       generated.GetString("arguments"));
}

ObjectReply DecideRealtimeMcpCall(ObjectStore& objects,
                                  const ChatPlanConfig& config,
                                  const ResponsesMcpDefinition& requested,
                                  const Json& expected_call,
                                  const Json& decision,
                                  const std::string& response_id,
                                  const StateAccess& access) {
  auto reply = Active(response_id, access);
  if (!Accepted(reply)) return reply;
  reply = Pending(expected_call);
  if (!Accepted(reply)) return reply;
  size_t bytes = 0;
  if (!MeasureRealtimeStorage(decision, 1ULL << 20, &bytes))
    return Error(413, "realtime_mcp_call_capacity",
                 "MCP decision exceeds capacity");
  ChatPlanError error;
  const auto status =
      ValidateRealtimeShape(decision, RealtimeShape::kItem, &error);
  if (!status) return Error(error.http_status, error.code, status.message());
  const auto id = decision.GetString("approval_request_id");
  if (decision.GetString("type") != "mcp_approval_response" ||
      !id.starts_with("mcpr_") || !ValidRequestId(id) ||
      !ValidRequestId(decision.GetString("id")) ||
      expected_call.GetString("approval_request_id") != id)
    return Invalid("Approval response differs from the pending call");
  ResponsesMcpDefinition definition;
  reply = Resolve(requested, config, access, &definition);
  if (!Accepted(reply)) return reply;
  ResponsesMcpState state(objects, config, response_id);
  const auto owned = state.Inspect(expected_call.GetString("id"), access);
  if (!Accepted(owned)) return owned;
  const auto* call = owned.value.Find("call");
  if (!call || !SameCall(expected_call, *call))
    return Invalid("Approval response differs from its sealed invocation");
  return state.Decide(id, decision.GetBool("approve"),
                      decision.GetString("reason"), definition, access);
}

ObjectReply ExecuteRealtimeMcpCall(ObjectStore& objects,
                                   const ChatPlanConfig& config,
                                   const ResponsesMcpDefinition& requested,
                                   const Json& call,
                                   const std::string& response_id,
                                   const StateAccess& access,
                                   RealtimeMcpExecution* output) {
  if (!output) return Invalid("Missing MCP execution output owner");
  auto reply = Active(response_id, access);
  if (!Accepted(reply)) return reply;
  RealtimeMcpCallProjection projected;
  ChatPlanError error;
  const auto status = ProjectRealtimeMcpCall(call, &projected, &error);
  if (!status) return Error(error.http_status, error.code, status.message());
  if (projected.phase != RealtimeMcpCallProjection::Phase::kPending)
    return Error(409, "mcp_dispatch_not_ready", "MCP call is already terminal");
  ResponsesMcpDefinition definition;
  reply = Resolve(requested, config, access, &definition);
  if (!Accepted(reply)) return reply;
  ResponsesMcpState state(objects, config, response_id);
  const auto id = call.GetString("id");
  const auto owned = state.Inspect(id, access);
  if (!Accepted(owned)) return owned;
  const auto* stored = owned.value.Find("call");
  const auto* owner = owned.value.Find("execution_owner");
  if (!stored || !SameCall(call, *stored))
    return Invalid("MCP execution differs from its sealed call");
  if (owned.value.GetString("state") != "ready")
    return Error(409, "mcp_dispatch_not_ready",
                 "MCP call is not ready for dispatch");
  if (!owner || !Same(*owner, CurrentResponseOwner()) ||
      owned.value.GetString("response_id") != response_id)
    return Error(409, "realtime_mcp_owner_conflict",
                 "MCP call belongs to another execution");
  if (owned.value.GetBool("credentials_required") &&
      !ResponsesMcpHasCredentials(definition))
    return Error(400, "mcp_credentials_required", "Resupply MCP credentials");
  const auto catalog_id = owned.value.GetString("catalog_id");
  const auto catalog = state.CatalogById(catalog_id, access);
  if (!Accepted(catalog)) return catalog;
  const auto authority = state.OpenCatalog(catalog.value, definition, access);
  if (!Accepted(authority)) return authority;
  const auto* tool = owned.value.Find("tool");
  const auto* original = owned.value.Find("declaration");
  if (!tool || !original || owned.value.GetString("caller") != "direct" ||
      !ResponsesMcpToolAllowed(*original, *tool) ||
      !ResponsesMcpToolAllowed(definition.declaration, *tool) ||
      !ResponsesToolAllows(*original, "direct") ||
      !ResponsesToolAllows(definition.declaration, "direct"))
    return Error(403, "mcp_tool_denied",
                 "MCP tool is outside current authority");
  if (ResponsesMcpApprovalRequired(definition.declaration, *tool) &&
      (!owned.value.Find("decision") ||
       !owned.value.Find("decision")->GetBool("approve")))
    return Error(403, "mcp_approval_required",
                 "Current policy requires approval");
  Json arguments;
  if (!Parse(call.GetString("arguments"), &arguments) || !arguments.IsObject())
    return Invalid("Invalid sealed MCP arguments");
  const auto private_session = state.Session(catalog_id, definition, access);
  if (!Accepted(private_session)) return private_session;
  const auto* saved_session = private_session.value.Find("session");
  if (!saved_session) return Invalid("Missing native MCP session state");
  auto revision = private_session.value.GetInt("revision");
  McpSession session;
  session.state = *saved_session;
  Json published_session = session.state;
  session.checkpoint = [&](const Json& value) {
    auto saved =
        state.BindSession(catalog_id, definition, revision, value, access);
    if (Accepted(saved)) {
      revision = saved.value.GetInt("revision");
      published_session = value;
    }
    return saved;
  };
  const auto fetch = config.mcp_policy    ? config.mcp_policy->fetch
                     : config.file_inputs ? config.file_inputs->fetch
                                          : FileFetchOptions{};
  McpTransportReceipt receipt;
  const auto invoke = [&] {
    return InvokeMcp(
        {access.scope, access.deadline, access.cancelled},
        definition.credentials, fetch, "tools/call",
        Object({{"name", *call.Find("name")}, {"arguments", arguments}}),
        [&] { return state.Permit(id, definition, access, &session.state); },
        &receipt, {}, &session, config.mcp_tunnels);
  };
  auto transport = invoke();
  if (transport.code == "mcp_session_binding_changed" &&
      !receipt.call_permitted) {
    // This exact native preflight has not performed I/O or changed permission.
    // Refresh only a private session bound to superseded credentials/policy.
    session.state = Json();
    transport = invoke();
  }
  if (transport.commit_unknown) return transport;
  const StateAccess cleanup{
      access.scope,
      std::chrono::steady_clock::now() + std::chrono::seconds(2),
      {}};
  auto completed =
      state.Finish(id, receipt, transport, cleanup,
                   receipt.call_permitted ? &session.state : nullptr);
  if (!Accepted(completed)) return completed;
  RealtimeMcpExecution next;
  next.item = completed.value;
  next.call_permitted = receipt.call_permitted;
  next.outcome_unknown = receipt.outcome_unknown;
  if (session.state.Has("protocol") &&
      !Same(published_session, session.state)) {
    const auto saved = state.BindSession(catalog_id, definition, revision,
                                         session.state, cleanup);
    next.session_saved = Accepted(saved);
    next.session_commit_unknown = saved.commit_unknown;
    if (!next.session_saved) next.session_error = saved.ClientCode();
  }
  *output = std::move(next);
  return completed;
}
}  // namespace q4t::server
