#include "q4t/server/mcp_tunnel_agent.h"

#include <algorithm>

#include "file_fetch_internal.h"
#include "mcp_transport_internal.h"
#include "object_protocol.h"
#include "q4t/server/input_contract.h"
#include "q4t/server/mcp_policy.h"

namespace q4t::server {
namespace {
using namespace object_detail;
bool Text(const Json* value, size_t limit) {
  return value && value->IsString() && !value->str.empty() &&
         value->str.size() <= limit &&
         std::all_of(value->str.begin(), value->str.end(),
                     [](unsigned char c) { return c >= 0x21 && c <= 0x7e; });
}
Status Invalid() {
  return Status::Fail("Invalid MCP tunnel agent configuration");
}
}  // namespace
std::string SerializeMcpTunnelAgentConfig(const McpTunnelAgentConfig& config) {
  auto headers = Object();
  for (const auto& [key, value] : config.target.headers)
    Set(&headers, key, String(value));
  auto target = Object(
      {{"url", String(config.target.url)}, {"headers", std::move(headers)}});
  if (!config.target.authorization.empty())
    Set(&target, "authorization", String(config.target.authorization));
  return OutputJson(Object(
      {{"version", Number(1)},
       {"api_base", String(config.api_base)},
       {"api_key", String(config.api_key)},
       {"tunnel_id", String(config.tunnel_id)},
       {"channel", String(config.channel)},
       {"control_fetch", file_detail::OptionsJson(config.control_fetch)},
       {"target_fetch", file_detail::OptionsJson(config.target_fetch)},
       {"target", std::move(target)},
       {"restart_limit", Number(config.restart_limit)}}));
}
Status ParseMcpTunnelAgentConfig(std::string_view raw,
                                 McpTunnelAgentConfig* output) {
  if (!output || raw.empty() || raw.size() > kMcpTunnelAgentConfigBytes)
    return Invalid();
  Json value;
  io::JsonParseLimits limits;
  limits.max_depth = 16;
  limits.max_values = 4096;
  limits.max_string_bytes = 16384;
  limits.reject_duplicate_keys = true;
  std::string field;
  if (!io::ParseJson(std::string(raw), &value, true, limits) ||
      !InputFields(value,
                   {"version", "api_base", "api_key", "tunnel_id", "channel",
                    "control_fetch", "target_fetch", "target", "restart_limit"},
                   &field) ||
      !value.Find("version") || !IsInputInteger(*value.Find("version")) ||
      value.GetInt("version") != 1 || !Text(value.Find("api_base"), 4096) ||
      !Text(value.Find("api_key"), 8192) ||
      !Text(value.Find("tunnel_id"), 128) ||
      !ValidApplicationId(value.GetString("tunnel_id")))
    return Invalid();
  McpTunnelAgentConfig next;
  next.api_base = value.GetString("api_base");
  while (next.api_base.ends_with('/')) next.api_base.pop_back();
  next.api_key = value.GetString("api_key");
  next.tunnel_id = value.GetString("tunnel_id");
  if (value.Has("channel")) {
    if (!Text(value.Find("channel"), 128)) return Invalid();
    next.channel = value.GetString("channel");
  }
  if (value.Has("restart_limit")) {
    if (!IsInputInteger(*value.Find("restart_limit")) ||
        value.GetInt("restart_limit") < 0 || value.GetInt("restart_limit") > 5)
      return Invalid();
    next.restart_limit = value.GetInt("restart_limit");
  }
  for (const auto* name : {"control_fetch", "target_fetch"}) {
    auto& policy = std::string_view(name) == "control_fetch"
                       ? next.control_fetch
                       : next.target_fetch;
    if (const auto* config = value.Find(name)) {
      auto network = *config;
      if (network.IsObject() && !network.Has("max_redirects"))
        Set(&network, "max_redirects", Number(0));
      const auto* redirects = network.Find("max_redirects");
      if (!redirects || !IsInputInteger(*redirects) ||
          !ParseFileFetchOptions(network, &policy).ok() ||
          policy.max_redirects != 0)
        return Invalid();
    }
    policy.max_redirects = 0;
  }
  file_detail::Url base;
  if (!file_detail::ParseUrl(next.api_base, &base) ||
      base.target.find('?') != base.target.npos ||
      !file_detail::AllowedUrl(base, next.control_fetch))
    return Invalid();
  const auto* target = value.Find("target");
  if (!target ||
      !InputFields(*target, {"url", "authorization", "headers"}, &field) ||
      !Text(target->Find("url"), 4096))
    return Invalid();
  next.target.url = target->GetString("url");
  if (target->Has("authorization")) {
    if (!Text(target->Find("authorization"), 8192)) return Invalid();
    next.target.authorization = target->GetString("authorization");
  }
  if (const auto* headers = target->Find("headers")) {
    if (!headers->IsObject()) return Invalid();
    for (const auto& [key, value] : headers->object) {
      if (!value.IsString()) return Invalid();
      next.target.headers.emplace(key, value.str);
    }
  }
  if (!mcp_detail::CheckEndpoint(next.target, next.target_fetch).ok())
    return Invalid();
  // Check the representation the isolated worker will actually receive. Added
  // defaults must fit the same aggregate string/value/byte budgets as input.
  const auto bytes = SerializeMcpTunnelAgentConfig(next);
  Json wire;
  if (bytes.size() > kMcpTunnelAgentConfigBytes ||
      !io::ParseJson(bytes, &wire, true, limits))
    return Invalid();
  next.sha256 = Digest(raw);
  *output = std::move(next);
  return {};
}
Status LoadMcpTunnelAgentConfig(const std::string& path,
                                McpTunnelAgentConfig* output) {
  std::string raw;
  const auto status = ReadMcpPrivateConfig(path, &raw);
  return status ? ParseMcpTunnelAgentConfig(raw, output) : status;
}
Json McpTunnelAgentDescription(const McpTunnelAgentConfig& config) {
  return Object(
      {{"profile", String("q4t-native-mcp-tunnel-agent-v1")},
       {"sha256", String(config.sha256)},
       {"control_tls", Boolean(config.api_base.starts_with("https://"))},
       {"target_tls", Boolean(config.target.url.starts_with("https://"))},
       {"session_limit", Number(16)},
       {"restarts_per_300_seconds", Number(config.restart_limit)},
       {"model_restart", Boolean(false)},
       {"external_runtime_required", Boolean(false)}});
}
}  // namespace q4t::server
