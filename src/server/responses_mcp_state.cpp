#include "q4t/server/responses_mcp_state.h"

#include <algorithm>
#include <array>
#include <cmath>
#include <ctime>
#include <set>

#include "object_protocol.h"
#include "q4t/server/chat_protocol_identity.h"
#include "q4t/server/chat_response_parser.h"
#include "q4t/server/http_response.h"
#include "q4t/server/input_snapshot_bindings.h"
#include "q4t/server/input_contract.h"
#include "q4t/server/mcp_content.h"
#include "q4t/server/responses_program.h"
#include "q4t/server/responses_mcp_binding.h"
#include "q4t/server/responses_item_shape.h"
#include "q4t/server/responses_owner.h"
#include "q4t/server/tool_result_schema.h"

namespace q4t::server {
namespace {
using namespace object_detail;
constexpr int64_t kLifetime = 30 * 24 * 3600;
constexpr size_t kPayloadBytes = kCryptoPlaintextLimit;
struct Plaintext {
  std::string bytes;
  ~Plaintext() { CryptoWipe(&bytes); }
};
ObjectReply Invalid(std::string message) {
  return Error(400, "invalid_mcp_state", std::move(message));
}
ObjectReply Active(const StateAccess& access) {
  if (access.scope.empty())
    return Error(503, "object_scope_unavailable",
                 "Authenticated scope required");
  if (access.cancelled && access.cancelled())
    return Error(499, "request_cancelled", "MCP request cancelled");
  if (std::chrono::steady_clock::now() >= access.deadline)
    return Error(408, "request_deadline_exceeded", "MCP request expired");
  return {};
}
StateCipherBinding Binding(const std::string& model, std::string_view kind,
                           const std::string& id) {
  return {model,
          Digest(std::string(identity::kTemplateReference) + ':' +
                 identity::kRenderer),
          "responses.mcp." + std::string(kind) + ".v1", id};
}
bool Same(const Json& left, const Json& right) {
  std::string a, b;
  return InputValueIdentity(left, &a) && InputValueIdentity(right, &b) &&
         a == b;
}
// Only known optional MCP item/tool metadata may be omitted. Schema values and
// results are compared as complete values; never accept a weakened schema.
bool PublicMatches(const Json& incoming, const Json& stored) {
  for (const auto& [key, value] : incoming.object) {
    const auto* original = stored.Find(key);
    if (!original) {
      if (key == "error" && value.IsNull()) continue;
      return false;
    }
    if (key == "tools" && incoming.GetString("type") == "mcp_list_tools") {
      if (!value.IsArray() || !original->IsArray() ||
          value.array.size() != original->array.size())
        return false;
      for (size_t i = 0; i < value.array.size(); ++i) {
        for (const auto& [field, member] : value.array[i].object) {
          const auto* old = original->array[i].Find(field);
          if (!old && member.IsNull() &&
              (field == "annotations" || field == "description"))
            continue;
          if (!old || !Same(member, *old)) return false;
        }
      }
    } else if (!Same(value, *original)) {
      return false;
    }
  }
  return true;
}
bool Unfinished(const Json& item) {
  const auto status = item.GetString("status");
  return item.GetString("type") == "mcp_call" &&
         (status == "in_progress" || status == "calling" ||
          status == "incomplete") &&
         (!item.Has("output") || item.Find("output")->IsNull()) &&
         (!item.Has("error") || item.Find("error")->IsNull());
}
ObjectReply Definition(const Json& original,
                       const ResponsesMcpDefinition& current,
                       const Json* tool = nullptr,
                       std::string_view caller = "direct") {
  ResponsesMcpDefinition old, now;
  ChatPlanError error;
  if (!PlanResponsesMcpDefinition(original, &old, &error) ||
      !PlanResponsesMcpDefinition(current.declaration, &now, &error) ||
      old.target_kind != now.target_kind || old.target_id != now.target_id ||
      old.declaration.GetString("server_label") !=
          now.declaration.GetString("server_label"))
    return Error(409, "mcp_server_changed",
                 "MCP server identity differs from the original authority");
  if (tool && (!ResponsesMcpToolAllowed(old.declaration, *tool) ||
               !ResponsesMcpToolAllowed(now.declaration, *tool) ||
               !ResponsesToolAllows(old.declaration, caller) ||
               !ResponsesToolAllows(now.declaration, caller)))
    return Error(403, "mcp_tool_denied",
                 "MCP tool is outside current authority");
  return {};
}
Json ExecutionError(std::string code, std::string message) {
  return Object(
      {{"type", String("mcp_tool_execution_error")},
       {"content", Object({{"code", String(std::move(code))},
                           {"message", String(std::move(message))}})}});
}
Json CallItem(const Json& payload) {
  const auto& call = *payload.Find("call");
  return Object({{"id", *call.Find("id")},
                 {"type", String("mcp_call")},
                 {"name", *call.Find("name")},
                 {"arguments", *call.Find("arguments")},
                 {"server_label", *call.Find("server_label")},
                 {"approval_request_id", *call.Find("approval_request_id")},
                 {"status", String("in_progress")},
                 {"output", Json()},
                 {"error", Json()}});
}
ObjectReply View(const Json& payload) {
  ObjectReply result;
  if (payload.GetString("state") == "approval") {
    const auto& call = *payload.Find("call");
    result.value = Object({{"id", *call.Find("approval_request_id")},
                           {"type", String("mcp_approval_request")},
                           {"name", *call.Find("name")},
                           {"arguments", *call.Find("arguments")},
                           {"server_label", *call.Find("server_label")}});
  } else if (const auto* terminal = payload.Find("terminal")) {
    result.value = *terminal;
  } else {
    result.value = CallItem(payload);
  }
  return result;
}
ObjectReply ValidateArguments(const Json& tool, const Json& arguments,
                              const StateAccess& access) {
  if (!arguments.IsObject()) return Invalid("MCP arguments must be an object");
  const auto checks = Array({Object({{"schema", *tool.Find("inputSchema")},
                                     {"output", String(OutputJson(arguments))},
                                     {"param", String("mcp.arguments")}})});
  return ValidateToolResultSchemas(
      checks, {access.scope, access.deadline, access.cancelled});
}
ObjectReply ResourceAuthority(const Json& payload, const std::string& uri,
                              const ResponsesMcpDefinition& current) {
  const auto* terminal = payload.Find("terminal");
  Json result;
  if (!McpResourceUri(uri) || !terminal ||
      !Parse(terminal->GetString("output"), &result) ||
      !ValidateMcpToolResult(result))
    return Error(403, "mcp_resource_denied", "No native resource authority");
  const auto& content = result.GetArray("content")->array;
  if (std::none_of(content.begin(), content.end(), [&](const auto& part) {
        return part.GetString("type") == "resource_link" &&
               part.GetString("uri") == uri;
      }))
    return Error(403, "mcp_resource_denied",
                 "Resource was not returned by this invocation");
  auto reply = Definition(*payload.Find("declaration"), current,
                          payload.Find("tool"), payload.GetString("caller"));
  if (!reply.ok()) return reply;
  if (payload.GetBool("credentials_required") &&
      !ResponsesMcpHasCredentials(current))
    return Error(400, "mcp_credentials_required",
                 "Resupply credentials before reading this MCP resource");
  const auto* decision = payload.Find("decision");
  if (ResponsesMcpApprovalRequired(current.declaration,
                                   *payload.Find("tool")) &&
      (!decision || !decision->GetBool("approve")))
    return Error(403, "mcp_approval_required",
                 "Current resource policy requires approved tool authority");
  return {};
}
Json* ResourceEntry(Json* payload, const std::string& uri) {
  if (!payload->GetArray("resources")) return nullptr;
  for (auto& entry : Field(*payload, "resources").array)
    if (entry.GetString("uri") == uri) return &entry;
  return nullptr;
}
ObjectReply ResourceView(const Json& entry) {
  ObjectReply reply;
  reply.value = Object({{"uri", *entry.Find("uri")},
                        {"state", *entry.Find("state")},
                        {"result", *entry.Find("result")},
                        {"error", *entry.Find("error")}});
  return reply;
}
Json ResourceError(std::string code, std::string message) {
  return Object({{"code", String(std::move(code))},
                 {"message", String(std::move(message))}});
}
size_t ResourceReserve(const Json& payload) {
  size_t bytes = 0;
  if (const auto* resources = payload.GetArray("resources"))
    for (const auto& entry : resources->array)
      if (entry.GetString("state") == "ready" ||
          entry.GetString("state") == "dispatching")
        bytes += 1024;  // Dispatch identity and bounded failure closure.
  return bytes;
}
bool EndedOwner(const Json& value, const StateAccess& access,
                const ResponseStatePlan* ended) {
  const auto* owner = value.Find("execution_owner");
  const bool proved = owner && ended && !ended->id.empty() &&
                      ended->scope == access.scope &&
                      value.GetString("response_id") == ended->id &&
                      ValidResponseOwner(ended->execution_owner) &&
                      Same(*owner, ended->execution_owner);
  return proved ||
         (owner && ResponseOwnerStatus(*owner) == ResponseOwnerState::kGone);
}
ObjectReply ModelProjection(const Json& payload,
                            McpContentProjection* projection) {
  *projection = {};
  const auto* terminal = payload.Find("terminal");
  if (!terminal)
    return Error(409, "mcp_result_pending", "No known MCP result to prepare");
  Json result;
  if (terminal->Find("output") && !terminal->Find("output")->IsNull()) {
    if (!Parse(terminal->GetString("output"), &result))
      return Invalid("Invalid native MCP result");
    const auto* resources = payload.GetArray("resources");
    const auto status = ProjectMcpToolResult(
        result, resources ? *resources : Array(), projection);
    if (!status) return Error(502, "mcp_content_invalid", status.message());
  } else {
    projection->content = Array(
        {Object({{"type", String("input_text")},
                 {"text", String("MCP invocation failed (untrusted):\n" +
                                 OutputJson(*terminal->Find("error")))}})});
  }
  return {};
}
}  // namespace

ObjectReply ResponsesMcpState::Read(const std::string& id,
                                    std::string_view kind,
                                    const StateAccess& access, Json* record,
                                    Json* payload) {
  auto reply = Active(access);
  if (!reply.ok()) return reply;
  if (!ValidId(id)) return Invalid("Invalid MCP state identifier");
  if (kind == "call" && id.starts_with("mcpr_")) {
    Json link_record, link;
    reply = Read(id, "approval", access, &link_record, &link);
    if (!reply.ok()) return reply;
    const auto call_id = link.GetString("call_id");
    if (!call_id.starts_with("mcp_") ||
        call_id != link_record.Find("data")->GetString("call_id"))
      return Invalid("Invalid MCP approval reference");
    reply = Read(call_id, "call", access, record, payload);
    if (!reply.ok()) return reply;
    if (payload->Find("call")->GetString("approval_request_id") != id)
      return Invalid("MCP approval does not belong to this call");
    return {};
  }
  ObjectRequest request;
  request.kind = ObjectKind::kToolRun;
  request.id = id;
  request.scope = access.scope;
  reply = objects_.Execute(request, access.deadline, access.cancelled);
  if (!reply.ok()) return reply;
  const auto* data = reply.value.Find("data");
  const auto expires = reply.value.GetInt("expires_at");
  if (!data ||
      data->GetString("format") != "q4t.mcp." + std::string(kind) + ".v1" ||
      !data->Find("sealed") || !data->Find("sealed")->IsString() ||
      expires <= std::time(nullptr))
    return Invalid("Invalid or expired MCP authority");
  Plaintext plain;
  auto opened = cipher_.Open(data->GetString("sealed"),
                             Binding(model_, kind, id), access, &plain.bytes);
  if (!opened.ok()) return opened;
  if (plain.bytes.size() > kPayloadBytes || !Parse(plain.bytes, payload) ||
      payload->GetInt("version") != 1 ||
      payload->GetInt("expires_at") != expires ||
      payload->GetString("id") != id || payload->GetString("kind") != kind)
    return Invalid("Invalid MCP authority payload");
  if (kind == "call") {
    const auto state = payload->GetString("state");
    const auto* call = payload->Find("call");
    const auto* tool = payload->Find("tool");
    if ((state != "approval" && state != "ready" && state != "dispatching" &&
         state != "completed" && state != "failed" && state != "denied" &&
         state != "unknown") ||
        !call || !call->IsObject() || !tool || !tool->IsObject() ||
        !payload->Find("declaration") || !tool->Find("inputSchema") ||
        !call->Find("approval_request_id") || !call->Find("arguments") ||
        !call->Find("name") || !call->Find("id") || !call->Find("server_label"))
      return Invalid("Invalid MCP invocation state");
    const auto* bindings = payload->Find("media_bindings");
    const auto* dependencies = data->Find("input_snapshots");
    if ((bindings &&
         (!bindings->IsArray() || bindings->array.size() > 4096 ||
          !payload->Find("media_complete") ||
          !payload->Find("media_complete")->IsBool() ||
          payload->GetString("media_projection_sha256").size() != 64)) ||
        !Same(bindings ? *bindings : Json(),
              dependencies ? *dependencies : Json()))
      return Invalid("MCP media dependencies differ from sealed authority");
    if (const auto* resources = payload->Find("resources")) {
      if (!resources->IsArray() || resources->array.size() > 32)
        return Invalid("Invalid MCP resource state capacity");
      std::set<std::string> uris;
      for (const auto& resource : resources->array) {
        const auto state = resource.GetString("state");
        const auto uri = resource.GetString("uri");
        const auto* owner = resource.Find("execution_owner");
        const auto* result = resource.Find("result");
        const auto* error = resource.Find("error");
        if (!McpResourceUri(uri) || !uris.insert(uri).second ||
            (state != "ready" && state != "dispatching" &&
             state != "completed" && state != "failed" && state != "unknown") ||
            !owner || !ValidResponseOwner(*owner) || !result || !error ||
            (!result->IsNull() && !result->IsObject()) ||
            (!error->IsNull() && !error->IsObject()) ||
            !resource.Find("response_id") ||
            !resource.Find("response_id")->IsString() ||
            (state == "dispatching" &&
             resource.GetString("dispatch_id").empty()))
          return Invalid("Invalid MCP resource preparation");
      }
    }
  }
  *record = std::move(reply.value);
  return {};
}
ObjectReply ResponsesMcpState::Write(const std::string& id,
                                     std::string_view kind, Json payload,
                                     int64_t revision, int64_t expires,
                                     const StateAccess& access) {
  auto reply = Active(access);
  if (!reply.ok()) return reply;
  Set(&payload, "version", Number(1));
  Set(&payload, "expires_at", Number(expires));
  Set(&payload, "id", String(id));
  Set(&payload, "kind", String(std::string(kind)));
  Plaintext plain;
  plain.bytes = OutputJson(payload);
  if (plain.bytes.size() > kPayloadBytes)
    return Error(413, "mcp_state_capacity",
                 "MCP authority exceeds byte capacity");
  std::string sealed;
  reply = cipher_.Seal(plain.bytes, Binding(model_, kind, id), access, &sealed);
  if (!reply.ok() || reply.commit_unknown) return reply;
  ObjectRequest request;
  request.operation = ObjectOperation::kPut;
  request.kind = ObjectKind::kToolRun;
  request.id = id;
  request.scope = access.scope;
  request.expected_revision = revision;
  request.expires_at = request.mutate_before = expires;
  request.data =
      Object({{"format", String("q4t.mcp." + std::string(kind) + ".v1")},
              {"sealed", String(std::move(sealed))}});
  if (kind == "call")
    if (const auto* bindings = payload.Find("media_bindings"))
      Set(&request.data, "input_snapshots", *bindings);
  const auto* call = payload.Find("call");
  const auto approval_id = call ? call->GetString("approval_request_id") : "";
  if (kind == "call" && revision == 0 && !approval_id.empty()) {
    // Keep the call at its public call ID. Approval is an atomic owned index;
    // standard history may omit the optional approval_request_id field.
    auto link = Object({{"version", Number(1)},
                        {"id", String(approval_id)},
                        {"kind", String("approval")},
                        {"expires_at", Number(expires)},
                        {"call_id", String(id)}});
    Plaintext linked;
    linked.bytes = OutputJson(link);
    std::string opaque;
    reply = cipher_.Seal(linked.bytes, Binding(model_, "approval", approval_id),
                         access, &opaque);
    if (!reply.ok() || reply.commit_unknown) return reply;
    auto alias = request;
    alias.id = approval_id;
    alias.data = Object({{"format", String("q4t.mcp.approval.v1")},
                         {"sealed", String(std::move(opaque))},
                         {"call_id", String(id)}});
    const std::array requests{request, alias};
    reply = objects_.ExecuteBatch(requests, access.deadline, access.cancelled);
    if (reply.ok() && !reply.commit_unknown) {
      const auto* rows = reply.value.GetArray("data");
      if (!rows || rows->array.size() != 2) {
        reply =
            Error(503, "mcp_state_commit_unknown", "MCP state receipt invalid");
        reply.commit_unknown = true;
      } else {
        auto created = rows->array.front();
        reply.value = std::move(created);
      }
    }
  } else {
    reply = objects_.Execute(request, access.deadline, access.cancelled);
  }
  if (reply.commit_unknown) {
    reply.code = "mcp_state_commit_unknown";
    reply.message = "MCP state commit unknown; no dispatch authorized";
    reply.value = Object({{"id", String(id)}});
  }
  return reply;
}
ObjectReply ResponsesMcpState::Catalog(const ResponsesMcpDefinition& definition,
                                       const Json& tools,
                                       const StateAccess& access,
                                       int64_t lifetime,
                                       std::string reserved_id,
                                       std::string listing_error) {
  if (lifetime < 1 || lifetime > kLifetime)
    return Invalid("Invalid MCP retention");
  Json projected, authority;
  ChatPlanError error;
  auto status = ProjectResponsesMcpCatalog(definition, tools, &projected,
                                           &authority, &error);
  if (!status) return Error(error.http_status, error.code, status.message());
  ResponsesMcpDefinition clean;
  status = PlanResponsesMcpDefinition(definition.declaration, &clean, &error);
  if (!status) return Invalid("Invalid MCP server declaration");
  if ((!reserved_id.empty() &&
       (!reserved_id.starts_with("mcpl_") || !ValidId(reserved_id))) ||
      listing_error.size() > 65536 ||
      (!listing_error.empty() && !authority.array.empty()))
    return Invalid("Invalid MCP catalog identity or failure");
  const auto id = reserved_id.empty() ? detail::NewRequestId("mcpl_")
                                      : std::move(reserved_id);
  if (id.empty())
    return Error(503, "mcp_entropy_unavailable", "MCP ID unavailable");
  auto item = Object({{"id", String(id)},
                      {"type", String("mcp_list_tools")},
                      {"server_label", *clean.declaration.Find("server_label")},
                      {"tools", std::move(projected)}});
  if (!listing_error.empty())
    Set(&item, "error", String(std::move(listing_error)));
  auto reply = Write(id, "catalog",
                     Object({{"public", item},
                             {"tools", std::move(authority)},
                             {"credentials_required",
                              Boolean(ResponsesMcpHasCredentials(definition))},
                             {"declaration", clean.declaration}}),
                     0, std::time(nullptr) + lifetime, access);
  if (reply.ok() && !reply.commit_unknown) reply.value = item;
  return reply;
}
ObjectReply ResponsesMcpState::Authenticate(const Json& item,
                                            const StateAccess& access) {
  ChatPlanError error;
  auto valid = ValidateResponsesItemShape(item, ResponsesItemShape::kInput,
                                          &error, "input.mcp");
  if (!valid) return Invalid(valid.message());
  const auto type = item.GetString("type");
  const bool listing = type == "mcp_list_tools";
  if (!listing && type != "mcp_call" && type != "mcp_approval_request" &&
      type != "mcp_approval_response")
    return Invalid("Expected MCP history item");
  const auto id = item.GetString(
      type == "mcp_approval_response" ? "approval_request_id" : "id");
  Json record, payload;
  auto reply =
      Read(id, listing ? "catalog" : "call", access, &record, &payload);
  // An interrupted argument fragment may never have reached Propose. It is
  // only assistant history with no result and no authority to run a tool.
  if (reply.http_status == 404 && Unfinished(item) &&
      item.GetString("status") == "incomplete") {
    reply = {};
    reply.value = item;
    return reply;
  }
  if (!reply.ok()) return reply;
  if (type == "mcp_call" && !payload.Has("terminal")) {
    auto recovered = Recover(id, access);
    if (!recovered.ok() || recovered.commit_unknown) return recovered;
    reply = Read(id, "call", access, &record, &payload);
    if (!reply.ok()) return reply;
  }
  if (type == "mcp_approval_response") {
    if (payload.Find("call")->GetString("approval_request_id") != id)
      return Invalid("Approval response does not match the original request");
    if (const auto* decision = payload.Find("decision");
        decision && decision->GetBool("approve") != item.GetBool("approve"))
      return Error(409, "mcp_approval_already_decided",
                   "Approval decision differs");
    reply.value = item;
    return reply;
  }
  Json expected;
  if (listing) {
    const auto* original = payload.Find("public");
    if (!original) return Invalid("Missing authenticated catalog");
    expected = *original;
  } else if (type == "mcp_approval_request") {
    if (payload.Find("call")->GetString("approval_request_id") != id)
      return Invalid("Approval request identity differs");
    Set(&payload, "state", String("approval"));
    expected = View(payload).value;
  } else {
    const auto* terminal = payload.Find("terminal");
    if (!terminal)
      return Error(409, "mcp_result_pending",
                   "MCP invocation has no terminal result");
    expected = *terminal;
  }
  auto compared = item;
  if (type == "mcp_call" && Unfinished(item)) {
    // Upgrade a no-result fragment only from its sealed terminal invocation.
    // Name, target label, original arguments and approval identity still match.
    for (const auto* key : {"status", "output", "error"})
      if (const auto* value = expected.Find(key)) Set(&compared, key, *value);
  }
  if (!PublicMatches(compared, expected))
    return Invalid("MCP item differs from its authenticated history");
  reply.value = std::move(expected);
  return reply;
}

ObjectReply ResponsesMcpState::OpenCatalog(
    const Json& catalog, const ResponsesMcpDefinition& current,
    const StateAccess& access) {
  auto authenticated = Authenticate(catalog, access);
  if (!authenticated.ok()) return authenticated;
  if (catalog.GetString("type") != "mcp_list_tools")
    return Invalid("Expected MCP catalog");
  Json record, payload;
  auto reply =
      Read(catalog.GetString("id"), "catalog", access, &record, &payload);
  if (!reply.ok()) return reply;
  const auto* declaration = payload.Find("declaration");
  if (!declaration || !payload.GetArray("tools"))
    return Invalid("Invalid private MCP catalog");
  reply = Definition(*declaration, current);
  if (reply.ok()) reply.value = std::move(payload);
  return reply;
}
ObjectReply ResponsesMcpState::Propose(
    const Json& catalog, const ResponsesMcpDefinition& current,
    std::string name, Json arguments, std::string caller,
    const StateAccess& access, std::string reserved_id,
    std::string original_arguments, const Json* program_origin) {
  Json record, payload;
  auto reply =
      Read(catalog.GetString("id"), "catalog", access, &record, &payload);
  if (!reply.ok()) return reply;
  const auto* original = payload.Find("public");
  const auto* tools = payload.GetArray("tools");
  const auto* declaration = payload.Find("declaration");
  if (!original || !tools || !declaration || !Same(*original, catalog))
    return Invalid("MCP list differs from its authenticated catalog");
  const Json* selected = nullptr;
  for (const auto& tool : tools->array)
    if (tool.GetString("name") == name) selected = &tool;
  if (!selected)
    return Error(403, "mcp_tool_denied", "Tool is not in the catalog");
  reply = Definition(*declaration, current, selected, caller);
  if (!reply.ok()) return reply;
  reply = ValidateArguments(*selected, arguments, access);
  if (!reply.ok()) return reply;
  if (!original_arguments.empty()) {
    Json original;
    if (original_arguments.size() > ChatResponseParser::kMaxToolBytes ||
        !Parse(original_arguments, &original) || !Same(original, arguments))
      return Invalid("Streamed MCP arguments differ from validated arguments");
  } else {
    original_arguments = OutputJson(arguments);
  }
  if (program_origin) {
    std::string param;
    const auto* index = program_origin->Find("index");
    const auto* call = program_origin->Find("call");
    const auto* after = call ? call->Find("after_results") : nullptr;
    const auto* positions = program_origin->GetArray("positions");
    if (caller != "programmatic" ||
        !InputFields(*program_origin,
                     {"program_id", "call_id", "index", "call", "positions"},
                     &param, "program") ||
        !program_origin->GetString("program_id").starts_with("prog_") ||
        !ValidId(program_origin->GetString("program_id")) ||
        !program_origin->GetString("call_id").starts_with("call_prog_") ||
        !ValidId(program_origin->GetString("call_id")) || !index ||
        !IsInputInteger(*index) || index->AsInt() < 0 || index->AsInt() >= 64 ||
        !call ||
        !InputFields(*call, {"key", "arguments", "after_results"}, &param,
                     "program.call") ||
        call->GetString("key") !=
            ResponsesMcpModelName(current.declaration.GetString("server_label"),
                                  name) ||
        call->GetString("arguments") != original_arguments || !after ||
        !IsInputInteger(*after) || after->AsInt() < 0 ||
        after->AsInt() > index->AsInt() || !positions ||
        positions->array.size() > 64 ||
        static_cast<size_t>(index->AsInt()) >= positions->array.size() ||
        !Same(positions->array[index->AsInt()], *after) ||
        reserved_id !=
            ResponsesMcpProgramCallId(program_origin->GetString("call_id"),
                                      index->AsInt()))
      return Invalid("MCP program invocation differs from its owned origin");
    for (size_t i = 0; i < positions->array.size(); ++i)
      if (!IsInputInteger(positions->array[i]) ||
          positions->array[i].AsInt() < 0 ||
          positions->array[i].AsInt() > static_cast<int64_t>(i))
        return Invalid("Invalid MCP program causal position");
  }
  const bool approval =
      ResponsesMcpApprovalRequired(*declaration, *selected) ||
      ResponsesMcpApprovalRequired(current.declaration, *selected);
  if (!reserved_id.empty() &&
      (!reserved_id.starts_with("mcp_") || !ValidId(reserved_id)))
    return Invalid("Invalid reserved MCP call identity");
  const auto call_id = reserved_id.empty() ? detail::NewRequestId("mcp_")
                                           : std::move(reserved_id);
  const auto approval_id = approval ? detail::NewRequestId("mcpr_") : "";
  if (call_id.empty() || (approval && approval_id.empty()))
    return Error(503, "mcp_entropy_unavailable", "MCP ID unavailable");
  const auto& id = call_id;
  auto next = Object(
      {{"state", String(approval ? "approval" : "ready")},
       {"execution_owner", CurrentResponseOwner()},
       {"response_id", String(response_id_)},
       {"catalog_id", *catalog.Find("id")},
       {"declaration", *declaration},
       {"credentials_required",
        Boolean(payload.GetBool("credentials_required") ||
                ResponsesMcpHasCredentials(current))},
       {"tool", *selected},
       {"caller", String(std::move(caller))},
       {"call", Object({{"id", String(call_id)},
                        {"name", String(std::move(name))},
                        {"arguments", String(std::move(original_arguments))},
                        {"server_label", *declaration->Find("server_label")},
                        {"approval_request_id",
                         approval ? String(approval_id) : Json()}})}});
  if (program_origin) Set(&next, "program_origin", *program_origin);
  reply = Write(id, "call", next, 0, record.GetInt("expires_at"), access);
  return reply.ok() && !reply.commit_unknown ? View(next) : reply;
}
ObjectReply ResponsesMcpState::CatalogById(const std::string& id,
                                           const StateAccess& access) {
  Json record, payload;
  auto reply = Read(id, "catalog", access, &record, &payload);
  if (!reply.ok()) return reply;
  const auto* item = payload.Find("public");
  if (!item) return Invalid("Missing authenticated MCP catalog");
  reply.value = *item;
  return reply;
}
ObjectReply ResponsesMcpState::Decide(const std::string& id, bool approve,
                                      std::string reason,
                                      const ResponsesMcpDefinition& current,
                                      const StateAccess& access) {
  if (reason.size() > 65536)
    return Invalid("MCP approval reason exceeds capacity");
  Json record, payload;
  auto reply = Read(id, "call", access, &record, &payload);
  if (!reply.ok()) return reply;
  if (payload.GetString("state") != "approval" ||
      payload.Find("call")->GetString("approval_request_id") != id)
    return Error(409, "mcp_approval_already_decided",
                 "MCP approval is not pending");
  reply = Definition(*payload.Find("declaration"), current,
                     payload.Find("tool"), payload.GetString("caller"));
  if (!reply.ok()) return reply;
  Set(&payload, "state", String(approve ? "ready" : "denied"));
  if (approve) {
    Set(&payload, "execution_owner", CurrentResponseOwner());
    Set(&payload, "response_id", String(response_id_));
  }
  Set(&payload, "decision",
      Object({{"approve", Boolean(approve)},
              {"reason", String(std::move(reason))}}));
  if (!approve) {
    auto item = CallItem(payload);
    Set(&item, "status", String("failed"));
    Set(&item, "error",
        ExecutionError("mcp_approval_denied", "Tool approval denied"));
    Set(&payload, "terminal", std::move(item));
  }
  reply = Write(record.GetString("id"), "call", payload,
                record.GetInt("revision"), record.GetInt("expires_at"), access);
  return reply.ok() && !reply.commit_unknown ? View(payload) : reply;
}
ObjectReply ResponsesMcpState::Permit(const std::string& id,
                                      const ResponsesMcpDefinition& current,
                                      const StateAccess& access) {
  Json record, payload;
  auto reply = Read(id, "call", access, &record, &payload);
  if (!reply.ok()) return reply;
  if (payload.GetString("state") != "ready")
    return Error(409, "mcp_dispatch_not_ready",
                 "MCP call cannot be dispatched again");
  if (payload.GetBool("credentials_required") &&
      !ResponsesMcpHasCredentials(current))
    return Error(400, "mcp_credentials_required",
                 "Resupply request credentials before invoking this MCP tool");
  reply = Definition(*payload.Find("declaration"), current,
                     payload.Find("tool"), payload.GetString("caller"));
  if (!reply.ok()) return reply;
  // A later request may tighten approval after a never-policy proposal.
  const auto* decision = payload.Find("decision");
  if (ResponsesMcpApprovalRequired(current.declaration,
                                   *payload.Find("tool")) &&
      (!decision || !decision->GetBool("approve")))
    return Error(403, "mcp_approval_required",
                 "Current policy requires approval");
  Set(&payload, "state", String("dispatching"));
  Set(&payload, "execution_owner", CurrentResponseOwner());
  Set(&payload, "response_id", String(response_id_));
  const auto dispatch_id = detail::NewRequestId("mcpd_");
  if (dispatch_id.empty())
    return Error(503, "mcp_entropy_unavailable", "MCP dispatch ID unavailable");
  Set(&payload, "dispatch_id", String(dispatch_id));
  reply = Write(record.GetString("id"), "call", payload,
                record.GetInt("revision"), record.GetInt("expires_at"), access);
  if (reply.ok() && !reply.commit_unknown)
    reply.value = Object({{"dispatch_id", String(dispatch_id)},
                          {"call", *payload.Find("call")},
                          {"tool", *payload.Find("tool")}});
  return reply;
}
ObjectReply ResponsesMcpState::Inspect(const std::string& id,
                                       const StateAccess& access) {
  Json record, payload;
  auto reply = Read(id, "call", access, &record, &payload);
  if (!reply.ok()) return reply;
  reply.value = std::move(payload);
  return reply;
}
ObjectReply ResponsesMcpState::ProposeResource(
    const std::string& id, const std::string& uri,
    const ResponsesMcpDefinition& current, const StateAccess& access) {
  Json record, payload;
  auto reply = Read(id, "call", access, &record, &payload);
  if (!reply.ok()) return reply;
  reply = ResourceAuthority(payload, uri, current);
  if (!reply.ok()) return reply;
  if (response_id_.size() > 256)
    return Invalid("Resource response identity exceeds capacity");
  if (ResourceEntry(&payload, uri))
    return Error(409, "mcp_resource_already_prepared",
                 "Resource preparation cannot be repeated");
  if (!payload.Has("resources")) Set(&payload, "resources", Array());
  auto& resources = Field(payload, "resources").array;
  if (resources.size() >= 32)
    return Error(413, "mcp_resource_capacity",
                 "Invocation resource preparation limit reached");
  resources.push_back(Object({{"uri", String(uri)},
                              {"state", String("ready")},
                              {"result", Json()},
                              {"error", Json()},
                              {"execution_owner", CurrentResponseOwner()},
                              {"response_id", String(response_id_)}}));
  if (OutputJson(payload).size() + ResourceReserve(payload) > kPayloadBytes)
    return Error(413, "mcp_resource_capacity",
                 "Resource preparation has no reserved completion capacity");
  auto view = ResourceView(resources.back());
  reply = Write(record.GetString("id"), "call", std::move(payload),
                record.GetInt("revision"), record.GetInt("expires_at"), access);
  return reply.ok() && !reply.commit_unknown ? view : reply;
}
ObjectReply ResponsesMcpState::Resource(const std::string& id,
                                        const std::string& uri,
                                        const StateAccess& access) {
  Json record, payload;
  auto reply = Read(id, "call", access, &record, &payload);
  if (!reply.ok()) return reply;
  const auto* entry = ResourceEntry(&payload, uri);
  return entry ? ResourceView(*entry)
               : Error(404, "mcp_resource_unprepared",
                       "No frozen resource preparation for this invocation");
}
ObjectReply ResponsesMcpState::ModelContent(const std::string& id,
                                            const StateAccess& access) {
  Json record, payload;
  auto reply = Read(id, "call", access, &record, &payload);
  if (!reply.ok()) return reply;
  McpContentProjection projection;
  reply = ModelProjection(payload, &projection);
  if (!reply.ok()) return reply;
  std::string digest;
  if (!InputValueIdentity(projection.content, &digest))
    return Error(413, "mcp_content_capacity",
                 "MCP content identity exceeds capacity");
  if (payload.Has("media_bindings") &&
      payload.GetString("media_projection_sha256") != digest)
    return Invalid("MCP model source changed after snapshot publication");
  auto unresolved = Array();
  for (const auto& uri : projection.unresolved_resources)
    unresolved.array.push_back(String(uri));
  reply.value =
      Object({{"content", std::move(projection.content)},
              {"unresolved_resources", std::move(unresolved)},
              {"revision", *record.Find("revision")},
              {"input_snapshots", payload.Has("media_bindings")
                                      ? *payload.Find("media_bindings")
                                      : Array()},
              {"complete", Boolean(payload.GetBool("media_complete"))}});
  return reply;
}
ObjectReply ResponsesMcpState::BindModelSnapshots(const std::string& id,
                                                  int64_t expected_revision,
                                                  const Json& bindings,
                                                  bool complete,
                                                  const StateAccess& access) {
  Json record, payload;
  auto reply = Read(id, "call", access, &record, &payload);
  if (!reply.ok()) return reply;
  if (expected_revision != record.GetInt("revision"))
    return Error(409, "mcp_media_revision_conflict", "MCP preparation changed");
  McpContentProjection projection;
  reply = ModelProjection(payload, &projection);
  if (!reply.ok()) return reply;
  if (!projection.unresolved_resources.empty())
    return Error(409, "mcp_resource_pending",
                 "Resolve linked resources before freezing media");
  std::string digest;
  if (!InputValueIdentity(projection.content, &digest))
    return Error(413, "mcp_content_capacity",
                 "MCP content identity exceeds capacity");
  std::set<std::string> expected;
  for (size_t i = 0; i < projection.content.array.size(); ++i) {
    const auto type = projection.content.array[i].GetString("type");
    if (type == "input_file" || type == "input_image")
      expected.insert("/content/" + std::to_string(i));
    else if (type != "input_text")
      return Error(503, "mcp_media_backend_unavailable",
                   "This native media requires its typed preparation backend");
  }
  Json normalized;
  const auto items =
      Array({Object({{"id", String(record.GetString("id"))},
                     {"content", std::move(projection.content)}})});
  reply = MergeInputSnapshots(bindings, items, &normalized);
  if (!reply.ok()) return reply;
  for (const auto& row : normalized.array)
    if (!expected.erase(row.GetString("path")))
      return Invalid("MCP snapshot does not bind a media source");
  if (!expected.empty())
    return Invalid("MCP media preparation omitted source snapshots");
  if (payload.Has("media_bindings") &&
      payload.GetString("media_projection_sha256") != digest)
    return Invalid("MCP media source identity changed");
  if (payload.GetBool("media_complete")) {
    if (!complete || !Same(normalized, *payload.Find("media_bindings")))
      return Error(409, "mcp_media_already_final",
                   "Frozen MCP model preparation is immutable");
    reply.value = Object({{"revision", *record.Find("revision")}});
    return reply;
  }
  Set(&payload, "media_bindings", std::move(normalized));
  Set(&payload, "media_projection_sha256", String(std::move(digest)));
  Set(&payload, "media_complete", Boolean(complete));
  if (OutputJson(payload).size() + ResourceReserve(payload) > kPayloadBytes)
    return Error(413, "mcp_media_capacity",
                 "MCP media bindings exceed shared capacity");
  reply = Write(record.GetString("id"), "call", std::move(payload),
                expected_revision, record.GetInt("expires_at"), access);
  if (reply.ok() && !reply.commit_unknown)
    reply.value = Object({{"revision", Number(expected_revision + 1)}});
  return reply;
}
ObjectReply ResponsesMcpState::PermitResource(
    const std::string& id, const std::string& uri,
    const ResponsesMcpDefinition& current, const StateAccess& access) {
  Json record, payload;
  auto reply = Read(id, "call", access, &record, &payload);
  if (!reply.ok()) return reply;
  reply = ResourceAuthority(payload, uri, current);
  if (!reply.ok()) return reply;
  if (response_id_.size() > 256)
    return Invalid("Resource response identity exceeds capacity");
  auto* entry = ResourceEntry(&payload, uri);
  if (!entry || entry->GetString("state") != "ready")
    return Error(409, "mcp_resource_not_ready",
                 "Resource read cannot be dispatched again");
  const auto dispatch = detail::NewRequestId("mcprd_");
  if (dispatch.empty())
    return Error(503, "mcp_entropy_unavailable",
                 "Resource dispatch ID unavailable");
  Set(entry, "state", String("dispatching"));
  Set(entry, "dispatch_id", String(dispatch));
  Set(entry, "execution_owner", CurrentResponseOwner());
  Set(entry, "response_id", String(response_id_));
  reply = Write(record.GetString("id"), "call", std::move(payload),
                record.GetInt("revision"), record.GetInt("expires_at"), access);
  if (reply.ok() && !reply.commit_unknown)
    reply.value =
        Object({{"dispatch_id", String(dispatch)}, {"uri", String(uri)}});
  return reply;
}
ObjectReply ResponsesMcpState::FinishResource(
    const std::string& id, const std::string& uri,
    const McpTransportReceipt& receipt, const ObjectReply& transport,
    const StateAccess& access) {
  Json record, payload;
  auto reply = Read(id, "call", access, &record, &payload);
  if (!reply.ok()) return reply;
  auto* entry = ResourceEntry(&payload, uri);
  if (!entry || (entry->GetString("state") != "ready" &&
                 entry->GetString("state") != "dispatching"))
    return Error(409, "mcp_resource_already_final",
                 "Resource read is already resolved or was never prepared");
  const bool dispatched = entry->GetString("state") == "dispatching";
  if ((dispatched &&
       (receipt.dispatch_id.empty() ||
        receipt.dispatch_id != entry->GetString("dispatch_id"))) ||
      (!dispatched && !receipt.dispatch_id.empty()))
    return Error(409, "mcp_resource_owner_conflict",
                 "Resource receipt belongs to another dispatch");
  const bool success = transport.ok() && !transport.commit_unknown;
  if ((success &&
       (!dispatched || !receipt.call_permitted || receipt.outcome_unknown)) ||
      (!dispatched && receipt.call_permitted))
    return Invalid("Resource result has no matching dispatch authority");
  const bool unknown =
      dispatched && (receipt.outcome_unknown || !receipt.call_permitted);
  Json error, result;
  if (!success) {
    error = ResourceError(unknown ? "mcp_resource_unknown" : transport.code,
                          unknown ? "Remote resource outcome unknown; no replay"
                                  : transport.message);
  } else if (!receipt.error.IsNull()) {
    // Keep the original RPC error as source data, not a successful resource.
    error = Object({{"code", String("mcp_resource_rpc_error")},
                    {"rpc_error", receipt.error}});
  } else if (!ValidateMcpResourcesResult(receipt.result)) {
    error = ResourceError("mcp_resource_invalid",
                          "Remote resources violate the native contract");
  } else {
    result = receipt.result;
  }
  Set(entry, "state",
      String(unknown          ? "unknown"
             : error.IsNull() ? "completed"
                              : "failed"));
  Set(entry, "result", std::move(result));
  Set(entry, "error", std::move(error));
  if (OutputJson(payload).size() + ResourceReserve(payload) > kPayloadBytes) {
    Set(entry, "state", String(unknown ? "unknown" : "failed"));
    Set(entry, "result", Json());
    Set(entry, "error",
        ResourceError(
            unknown ? "mcp_resource_unknown" : "mcp_resource_capacity",
            unknown ? "Remote resource outcome unknown; no replay"
                    : "Frozen resource exceeds invocation capacity"));
  }
  // Raw tool outcome is already durable and never rewritten by preparation.
  auto view = ResourceView(*entry);
  reply = Write(record.GetString("id"), "call", std::move(payload),
                record.GetInt("revision"), record.GetInt("expires_at"), access);
  return reply.ok() && !reply.commit_unknown ? view : reply;
}
ObjectReply ResponsesMcpState::Recover(const std::string& id,
                                       const StateAccess& access,
                                       const ResponseStatePlan* ended) {
  Json record, payload;
  auto reply = Read(id, "call", access, &record, &payload);
  if (!reply.ok()) return reply;
  if (const auto* terminal = payload.Find("terminal")) {
    const auto item = *terminal;
    bool changed = false;
    if (payload.GetArray("resources"))
      for (auto& resource : Field(payload, "resources").array) {
        const auto state = resource.GetString("state");
        if ((state != "ready" && state != "dispatching") ||
            !EndedOwner(resource, access, ended))
          continue;
        const bool dispatched = state == "dispatching";
        Set(&resource, "state", String(dispatched ? "unknown" : "failed"));
        Set(&resource, "error",
            ResourceError(dispatched ? "mcp_resource_unknown"
                                     : "mcp_resource_interrupted",
                          dispatched ? "Resource owner ended; no replay"
                                     : "Resource owner ended before dispatch"));
        changed = true;
      }
    if (changed) {
      reply =
          Write(record.GetString("id"), "call", std::move(payload),
                record.GetInt("revision"), record.GetInt("expires_at"), access);
      if (!reply.ok() || reply.commit_unknown) return reply;
    }
    reply.value = item;
    return reply;
  }
  const auto state = payload.GetString("state");
  if (state != "ready" && state != "dispatching")
    return Error(409, "mcp_result_pending", "MCP approval is still pending");
  const auto* owner = payload.Find("execution_owner");
  const bool own_ended = owner && ended && !ended->id.empty() &&
                         ended->scope == access.scope &&
                         payload.GetString("response_id") == ended->id &&
                         ValidResponseOwner(ended->execution_owner) &&
                         Same(*owner, ended->execution_owner);
  if (!own_ended &&
      (!owner || ResponseOwnerStatus(*owner) != ResponseOwnerState::kGone))
    return Error(409, "mcp_result_pending",
                 "MCP execution owner is live or cannot be verified");
  auto item = CallItem(payload);
  const bool dispatched = state == "dispatching";
  Set(&item, "status", String("failed"));
  Set(&item, "error",
      ExecutionError(
          dispatched ? "mcp_result_unknown" : "mcp_execution_interrupted",
          dispatched ? "Execution owner ended; remote outcome unknown, "
                       "call will not be replayed"
                     : "Execution owner ended before remote dispatch"));
  Set(&payload, "state", String(dispatched ? "unknown" : "failed"));
  Set(&payload, "terminal", item);
  reply = Write(record.GetString("id"), "call", payload,
                record.GetInt("revision"), record.GetInt("expires_at"), access);
  if (reply.ok() && !reply.commit_unknown) reply.value = std::move(item);
  // A concurrent known completion wins. Do not overwrite it or retry a write.
  if (!reply.commit_unknown && reply.code == "object_revision_conflict") {
    auto current = Inspect(id, access);
    if (current.ok() && current.value.Has("terminal")) {
      auto terminal = *current.value.Find("terminal");
      current.value = std::move(terminal);
      return current;
    }
  }
  return reply;
}
ObjectReply ResponsesMcpState::Reconcile(Json* items,
                                         const ResponseStatePlan& ended,
                                         const StateAccess& access) {
  if (!items || !items->IsArray()) return Invalid("Expected MCP output items");
  for (auto& item : items->array) {
    if (!Unfinished(item) && !(item.GetString("type") == "mcp_call" &&
                               (item.GetString("status") == "completed" ||
                                item.GetString("status") == "failed")))
      continue;
    auto recovered = Recover(item.GetString("id"), access, &ended);
    if (recovered.commit_unknown) return recovered;
    // An argument fragment may predate Propose. A sibling request may also
    // own an approval dispatch now; neither gives this cleanup authority.
    if (recovered.http_status == 404 || recovered.code == "mcp_result_pending")
      continue;
    if (!recovered.ok()) return recovered;
    auto authenticated = Authenticate(item, access);
    if (!authenticated.ok()) return authenticated;
    item = std::move(authenticated.value);
  }
  return {};
}
ObjectReply ResponsesMcpState::Finish(const std::string& id,
                                      const McpTransportReceipt& receipt,
                                      const ObjectReply& transport,
                                      const StateAccess& access) {
  Json record, payload;
  auto reply = Read(id, "call", access, &record, &payload);
  if (!reply.ok()) return reply;
  const auto state = payload.GetString("state");
  if (state != "dispatching" && state != "ready")
    return Error(409, "mcp_result_already_final",
                 "MCP invocation is already resolved");
  // A losing concurrent caller's transport failure is not evidence about the
  // winner. Even failure/unknown completion must belong to this dispatch.
  if ((state == "dispatching" &&
       (receipt.dispatch_id.empty() ||
        receipt.dispatch_id != payload.GetString("dispatch_id"))) ||
      (state == "ready" && !receipt.dispatch_id.empty()))
    return Error(409, "mcp_dispatch_owner_conflict",
                 "MCP result belongs to a different dispatch owner");
  const bool success = transport.ok() && !transport.commit_unknown;
  if (success && (state != "dispatching" || !receipt.call_permitted ||
                  receipt.outcome_unknown))
    return Invalid("Successful result has no confirmed dispatch authority");
  if (state == "ready" && receipt.call_permitted)
    return Invalid("Remote call has no matching dispatch record");
  if (success) {
    if (!receipt.error.IsNull()) {
      const auto* code = receipt.error.Find("code");
      const auto* message = receipt.error.Find("message");
      if (!receipt.error.IsObject() || !code || !code->IsNumber() ||
          !std::isfinite(code->number) ||
          std::trunc(code->number) != code->number || !message ||
          !message->IsString())
        return Invalid("Malformed MCP protocol error receipt");
    }
  }
  auto item = CallItem(payload);
  Json error;
  bool unknown = state == "dispatching" &&
                 (receipt.outcome_unknown || !receipt.call_permitted);
  if (!success) {
    if (transport.code == "mcp_http_error") {
      error = Object(
          {{"type", String("http_error")},
           {"code", Number(transport.value.GetInt("upstream_status"))},
           {"message",
            String(unknown ? "Remote HTTP failure; outcome unknown, no replay"
                           : transport.message)}});
    } else {
      error = ExecutionError(
          unknown ? "mcp_result_unknown" : transport.code,
          unknown ? "Remote outcome unknown; call will not be replayed"
                  : transport.message);
    }
  } else if (!receipt.error.IsNull()) {
    error = Object({{"type", String("mcp_protocol_error")},
                    {"code", *receipt.error.Find("code")},
                    {"message", *receipt.error.Find("message")}});
  } else if (!ValidateMcpToolResult(receipt.result)) {
    // A complete response with bad content is still a known remote outcome.
    // Close the owned dispatch; do not leave it pending or authorize replay.
    const bool capacity = OutputJson(receipt.result).size() > kPayloadBytes;
    error =
        ExecutionError(capacity ? "mcp_result_capacity" : "mcp_result_invalid",
                       "MCP result violates the native content contract");
  } else if (receipt.result.GetBool("isError")) {
    error = Object({{"type", String("mcp_tool_execution_error")},
                    {"content", *receipt.result.Find("content")}});
    // A tool-reported error is a known native result, including structured
    // data, media and metadata. Preserve it independently of the public error
    // discriminator, which has no field for structuredContent.
    Set(&item, "output", String(OutputJson(receipt.result)));
  } else {
    const auto* schema = payload.Find("tool")->Find("outputSchema");
    if (schema) {
      const auto* output = receipt.result.Find("structuredContent");
      if (!output) {
        error = ExecutionError("mcp_result_schema",
                               "Required structured result missing");
      } else {
        auto checked = ValidateToolResultSchemas(
            Array({Object({{"schema", *schema},
                           {"output", String(OutputJson(*output))},
                           {"param", String("mcp.structuredContent")}})}),
            {access.scope, access.deadline, access.cancelled});
        if (!checked.ok())
          error = ExecutionError(checked.code, checked.message);
      }
    }
    if (error.IsNull())
      Set(&item, "output", String(OutputJson(receipt.result)));
  }
  const bool completed = error.IsNull();
  Set(&item, "status", String(completed ? "completed" : "failed"));
  Set(&item, "error", std::move(error));
  Set(&payload, "state",
      String(unknown     ? "unknown"
             : completed ? "completed"
                         : "failed"));
  Set(&payload, "terminal", item);
  if (OutputJson(payload).size() > kPayloadBytes) {
    Set(&item, "output", Json());
    Set(&item, "status", String("failed"));
    Set(&item, "error",
        ExecutionError("mcp_result_capacity",
                       "MCP result exceeds durable capacity"));
    Set(&payload, "state", String("failed"));
    Set(&payload, "terminal", item);
  }
  reply = Write(record.GetString("id"), "call", payload,
                record.GetInt("revision"), record.GetInt("expires_at"), access);
  if (reply.ok() && !reply.commit_unknown) reply.value = std::move(item);
  return reply;
}
}  // namespace q4t::server
