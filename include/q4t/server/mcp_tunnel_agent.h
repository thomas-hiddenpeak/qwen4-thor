// Optional native private-side MCP tunnel agent. Never starts/restarts a model.
#pragma once

#include "q4t/server/mcp_transport.h"
#include "q4t/status.h"

namespace q4t::server {
inline constexpr size_t kMcpTunnelAgentConfigBytes = 64U << 10;
struct McpTunnelAgentConfig {
  std::string api_base, api_key, tunnel_id, channel = "main", sha256;
  FileFetchOptions control_fetch, target_fetch;
  McpEndpoint target;
  // Initial launch plus at most this many recovery launches per five minutes.
  unsigned restart_limit = 3;
};
Status ParseMcpTunnelAgentConfig(std::string_view raw,
                                 McpTunnelAgentConfig* output);
Status LoadMcpTunnelAgentConfig(const std::string& path,
                                McpTunnelAgentConfig* output);
// Private worker transport only: contains credentials; never log this value.
std::string SerializeMcpTunnelAgentConfig(const McpTunnelAgentConfig& config);
io::Json McpTunnelAgentDescription(const McpTunnelAgentConfig& config);
// Current foreground process supervises only its own isolated agent child.
// Cancellation is intentional stop and cannot spend a restart attempt.
int RunMcpTunnelAgent(const McpTunnelAgentConfig& config,
                      const std::function<bool()>& stop);
int RunMcpTunnelAgentCommand(int argc, char** argv);
int MaybeRunMcpTunnelAgentWorker(int argc, char** argv);
}  // namespace q4t::server
