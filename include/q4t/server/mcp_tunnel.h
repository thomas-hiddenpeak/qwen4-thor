// Native reverse-MCP queue. The caller owns authentication and lifecycle.
// No external I/O, model restart, task replay or persistent credential store.
#pragma once

#include "q4t/server/binary_store.h"
#include "q4t/status.h"

namespace q4t::server {
struct McpTunnelRoute {
  std::string id;
  std::string generation;
  std::vector<std::string> callers;
  std::vector<std::string> agents;
  std::vector<std::string> channels{"main"};
};
struct McpTunnelLimits {
  size_t requests = 128;
  size_t per_client = 16;
  uint64_t retained_bytes = 32U << 20;
  int request_ms = 120000;
  int agent_lease_ms = 30000;
};
class McpTunnelBroker {
 public:
  McpTunnelBroker();
  ~McpTunnelBroker();
  McpTunnelBroker(const McpTunnelBroker&) = delete;
  McpTunnelBroker& operator=(const McpTunnelBroker&) = delete;
  // Immutable once configured. Failure leaves the broker unconfigured.
  Status Configure(std::vector<McpTunnelRoute> routes,
                   McpTunnelLimits limits = {});
  // Submit is internal, after original MCP authority is validated. It does
  // not grant any approval itself. Headers are private MCP credentials only;
  // the agent's fixed target never comes from this message.
  ObjectReply Submit(const BinaryAccess& caller, std::string_view tunnel,
                     std::string_view generation, std::string_view channel,
                     const io::Json& headers, const io::Json& rpc,
                     bool terminate_session = false,
                     std::string_view expected_instance = {});
  // An authenticated agent has a stable random instance ID for its lifetime.
  // A bounded exclusive lease prevents stateful sessions from being spread
  // across competing instances sharing the same credential.
  ObjectReply Poll(const BinaryAccess& agent, std::string_view tunnel,
                   std::string_view instance,
                   const std::vector<std::string>& channels = {},
                   size_t limit = 25, int wait_ms = 15000);
  ObjectReply Respond(const BinaryAccess& agent, std::string_view tunnel,
                      std::string_view instance, std::string_view shard_token,
                      const io::Json& response);
  // Private transport result, including whether the task was handed off.
  // One caller consumes completion. Removed/expired tasks are never requeued.
  ObjectReply Wait(const BinaryAccess& caller, std::string_view request_id);
  ObjectReply Describe(const BinaryAccess& access, std::string_view tunnel);
  io::Json Health() const;
  // Wakes owned waiters. No callbacks are retained or invoked under a mutex.
  void Stop();

 private:
  struct Impl;
  std::unique_ptr<Impl> impl_;
};
}  // namespace q4t::server
