#include "agent_session_internal.h"

#include <algorithm>

#include "mcp_transport_internal.h"
#include "object_protocol.h"
#include "q4t/server/request_json.h"
#include "q4t/server/responses_execution.h"
#include "vault_credential_internal.h"

namespace q4t::server {
namespace {
using namespace object_detail;

StateCipherBinding TransportBinding(std::string_view session) {
  return {"q4t-agent", std::string(session), "session-mcp-http", "transports"};
}

ObjectReply InvalidSettings() {
  return Error(503, "agent_session_record_invalid",
               "Invalid session MCP settings");
}
}  // namespace

ObjectReply agent_detail::SealAgentHttp(ObjectStore& objects,
                                        const StateAccess& access,
                                        std::string_view session_id,
                                        const Json& settings,
                                        std::string* sealed) {
  sealed->clear();
  if (settings.IsNull() || settings.object.empty()) return {};
  auto wire = OutputJson(settings);
  auto result = StateCipher(objects).Seal(wire, TransportBinding(session_id),
                                          access, sealed);
  CryptoWipe(&wire);
  return result;
}

void ResponsesExecution::ConfigureAgentMcp(const Json& session,
                                           std::string sealed_http) {
  agent_http_session_ = session.GetString("id");
  agent_http_sealed_ = std::move(sealed_http);
  agent_vaults_ = *session.Find("vault_ids");
  agent_mcp_tools_ = Array();
  for (const auto& tool : session.Find("agent")->GetArray("tools")->array)
    if (tool.GetString("type") == "mcp") agent_mcp_tools_.array.push_back(tool);
}

ObjectReply ResponsesExecution::RestoreAgentMcp() {
  if (plan_.agent_session_id.empty()) return {};
  auto row = agent_detail::ReadRow(objects_, plan_.agent_session_id, access_);
  if (!row.ok()) return row;
  const auto* data = row.value.Find("data");
  const auto* turns = data ? data->GetArray("turns") : nullptr;
  if (!turns || turns->array.empty()) return InvalidSettings();
  const auto& step = turns->array.back();
  if (step.GetString("response_id") != Id()) return InvalidSettings();
  auto settings = agent_detail::SessionSettings(*data, &step);
  if (!settings.ok()) return settings;
  if (!settings.value.GetArray("vault_ids")) return InvalidSettings();
  ConfigureAgentMcp(settings.value, step.GetString("sealed_http"));
  return {};
}

ObjectReply ResponsesExecution::AuthorizeAgentMcp(
    ResponsesMcpDefinition* definition, bool dispatch) {
  if (agent_http_session_.empty()) return {};
  const auto label = definition->declaration.GetString("server_label");
  const auto found =
      std::ranges::find_if(agent_mcp_tools_.array, [&](const auto& tool) {
        return tool.GetString("server_label") == label;
      });
  if (found == agent_mcp_tools_.array.end() ||
      definition->target_kind != "server_url" ||
      found->Find("transport")->GetString("server_url") !=
          definition->target_id)
    return Error(403, "mcp_target_denied",
                 "MCP server is outside this session");
  auto& endpoint = definition->credentials;
  endpoint.headers.clear();
  bool needs_refresh = false;
  auto resolved = vault_detail::ResolveMcpToken(
      objects_, access_, agent_vaults_, found->GetString("credential_id"),
      definition->target_id, &endpoint.authorization, nullptr, &needs_refresh);
  if (!resolved.ok()) return resolved;
  if (!agent_http_sealed_.empty()) {
    std::string wire;
    auto opened =
        cipher_.Open(agent_http_sealed_, TransportBinding(agent_http_session_),
                     access_, &wire);
    if (!opened.ok()) return opened;
    Json overrides;
    auto parsed = io::ParseJson(wire, &overrides, true, kSchemaJsonLimits);
    CryptoWipe(&wire);
    if (!parsed || !overrides.IsObject()) return InvalidSettings();
    if (const auto* transport = overrides.Find(label)) {
      if (transport->GetString("server_url") != definition->target_id)
        return InvalidSettings();
      if (const auto* auth = transport->Find("authorization");
          auth && !auth->IsNull()) {
        if (!endpoint.authorization.empty() || needs_refresh)
          return Error(
              400, "mcp_credential_conflict",
              "Choose either inline authorization or a Vault credential");
        endpoint.headers.emplace("Authorization", auth->str);
      }
      if (const auto* headers = transport->Find("headers"))
        for (const auto& [key, val] : headers->object)
          endpoint.headers.emplace(key, val.str);
    }
  }
  endpoint.exact_target = needs_refresh || !endpoint.authorization.empty() ||
                          !endpoint.headers.empty();
  if (endpoint.exact_target && !definition->target_id.starts_with("https://"))
    return Error(400, "mcp_credentials_require_tls",
                 "Authenticated MCP requires HTTPS");
  endpoint.request_metadata = *found->Find("request_metadata");
  definition->required = found->GetBool("required");
  auto checked = mcp_detail::CheckHeaders(endpoint);
  if (!checked.ok() || !needs_refresh || !dispatch) return checked;
  checked = mcp_detail::CheckEndpoint(endpoint, mcp_fetch_);
  if (!checked.ok()) return checked;
  return vault_detail::ResolveMcpToken(
      objects_, access_, agent_vaults_, found->GetString("credential_id"),
      definition->target_id, &endpoint.authorization, &mcp_fetch_);
}
}  // namespace q4t::server
