// The private-side agent is independent of model and service supervision.
// All DNS/TLS/external I/O belongs to its independently bounded process.
#pragma once

#include "q4t/server/mcp_transport.h"

namespace q4t::server::mcp_detail {
// Optional wire timeout; malformed/absent values retain the local policy cap.
std::chrono::steady_clock::time_point TunnelCommandDeadline(
    const io::Json& command, std::chrono::steady_clock::time_point received,
    std::chrono::milliseconds local_limit);
class TunnelAgentBackend {
 public:
  TunnelAgentBackend(McpEndpoint fixed_target, FileFetchOptions network);
  ~TunnelAgentBackend();
  // Fixed target/policy come from private operator configuration. The command
  // supplies only MCP headers/RPC, never a URL, executable or network policy.
  // Always emits a terminal wire response for an identifiable command; a
  // transport failure is not permission to replay the external operation.
  ObjectReply Exchange(
      const io::Json& command, std::chrono::steady_clock::time_point deadline,
      const std::function<bool(const io::Json&)>& progress = {});
  void Collect();
  void Clear();

 private:
  struct Impl;
  std::unique_ptr<Impl> impl_;
};
}  // namespace q4t::server::mcp_detail
