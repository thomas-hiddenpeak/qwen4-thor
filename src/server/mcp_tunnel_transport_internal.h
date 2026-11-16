// Parent-owned native transport authority. No external sockets or retained
// request callbacks. Callers serialize access with the MCP owner mutex.
#pragma once

#include "q4t/server/mcp_transport.h"
#include "q4t/server/mcp_tunnel.h"

namespace q4t::server::mcp_detail {
class TunnelExchange;
class TunnelSessions {
 public:
  TunnelSessions();
  ~TunnelSessions();
  ObjectReply Begin(const BinaryAccess& access, const McpEndpoint& endpoint,
                    const std::shared_ptr<McpTunnelBroker>& broker,
                    std::string_view method, const io::Json& params,
                    const McpSession* session,
                    std::unique_ptr<TunnelExchange>* exchange);
  // Cleanup submits a bounded, best-effort termination without waiting for
  // an agent. Missing telemetry/agents never stop the healthy model.
  void Collect();
  void Clear(bool release_remote);

 private:
  friend class TunnelExchange;
  struct Impl;
  std::unique_ptr<Impl> impl_;
};
class TunnelExchange {
 public:
  ~TunnelExchange();
  ObjectReply Forward(const io::Json& wire, bool permitted);
  ObjectReply Checkpoint(const io::Json& state);
  void Retain();

 private:
  friend class TunnelSessions;
  struct Impl;
  explicit TunnelExchange(std::unique_ptr<Impl> impl);
  std::unique_ptr<Impl> impl_;
};
// Worker-only synchronous bridge; never called by idle collection or Close.
ObjectReply TunnelWorkerExchange(const io::Json& headers, const io::Json& rpc,
                                 std::string_view expected_instance);
}  // namespace q4t::server::mcp_detail
