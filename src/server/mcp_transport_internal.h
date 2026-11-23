#pragma once

#include <ctime>

#include "q4t/server/mcp_transport.h"

namespace q4t::server::mcp_detail {
// Private worker operation for a Vault-owned grant, never an MCP JSON-RPC.
inline constexpr std::string_view kRefreshGrant = "vault/oauth-refresh";
ObjectReply BuildGrant(const io::Json& grant,
                       std::map<std::string, std::string>* headers,
                       std::string* body);
ObjectReply DecodeGrant(const std::string& response, std::time_t now,
                        io::Json* destination);
ObjectReply DecodeGrantRefusal(int status, const std::string& response,
                               std::string_view retry_after, std::time_t now,
                               McpTransportReceipt* receipt);
ObjectReply RefreshGrant(const McpEndpoint& endpoint,
                         const FileFetchOptions& deployment,
                         const io::Json& grant,
                         std::chrono::steady_clock::time_point deadline,
                         const std::function<bool()>& permit,
                         McpTransportReceipt* receipt);
ObjectReply CheckHeaders(const McpEndpoint& endpoint);
ObjectReply CheckEndpoint(const McpEndpoint& endpoint,
                          const FileFetchOptions& deployment);
ObjectReply Exchange(const McpEndpoint& endpoint,
                     const FileFetchOptions& deployment,
                     std::string_view method, const io::Json& params,
                     std::chrono::steady_clock::time_point deadline,
                     const std::function<bool()>& permit_call,
                     McpTransportReceipt* receipt);
// Only the isolated native worker owns connections. At most 16 sessions,
// including at most 4 per authenticated scope, with bounded idle/lifetimes.
class Sessions {
 public:
  Sessions();
  ~Sessions();
  ObjectReply Exchange(const McpEndpoint& endpoint,
                       const FileFetchOptions& deployment,
                       std::string_view method, const io::Json& params,
                       std::chrono::steady_clock::time_point deadline,
                       io::Json* session, bool allow_new, int idle_ms,
                       int lifetime_ms,
                       const std::function<bool(const io::Json&)>& checkpoint,
                       const std::function<bool()>& permit_call,
                       McpTransportReceipt* receipt);
  void Collect();
  void Clear(bool release_remote = true);
  size_t Size() const;

 private:
  struct Impl;
  std::unique_ptr<Impl> impl_;
};
}  // namespace q4t::server::mcp_detail
