#pragma once

#include "q4t/server/mcp_transport.h"

namespace q4t::server::mcp_detail {
ObjectReply CheckHeaders(const McpEndpoint& endpoint);
ObjectReply CheckEndpoint(const McpEndpoint& endpoint,
                          const FileFetchOptions& deployment);
ObjectReply Exchange(const McpEndpoint& endpoint,
                     const FileFetchOptions& deployment,
                     std::string_view method, const io::Json& params,
                     std::chrono::steady_clock::time_point deadline,
                     const std::function<bool()>& permit_call,
                     McpTransportReceipt* receipt);
}  // namespace q4t::server::mcp_detail
