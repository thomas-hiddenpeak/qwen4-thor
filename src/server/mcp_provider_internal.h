#pragma once

#include "q4t/server/mcp_transport.h"

namespace q4t::server::file_detail {
class RemoteHttp;
}
namespace q4t::server::mcp_detail {
struct ProviderRequest {
  // Constructed by a native adapter, never copied from tool arguments.
  std::string url;
  bool token_query = false;
  bool binary = false;
  // Adapter-created resource-key headers, never arbitrary tool arguments.
  std::map<std::string, std::string> headers{};
};
using ProviderFetch =
    std::function<ObjectReply(const ProviderRequest&, io::Json*)>;

bool NativeProvider(std::string_view id);
bool ProviderOrigin(std::string_view id, std::string_view url);
std::string ProviderEscape(std::string_view value);
ObjectReply ReadProviderResponse(file_detail::RemoteHttp& response,
                                 uint64_t* total_bytes, io::Json* output,
                                 bool binary = false);
// The callback is supplied by native code. Production uses the independently
// owned MCP worker and fixed-origin HTTPS; tests can exercise real local peers.
ObjectReply InvokeProvider(std::string_view id, std::string_view method,
                           const io::Json& params, const ProviderFetch& fetch,
                           io::Json* result, io::Json* error);
ObjectReply ExchangeProvider(const McpEndpoint& endpoint,
                             const FileFetchOptions& deployment,
                             std::string_view method, const io::Json& params,
                             std::chrono::steady_clock::time_point deadline,
                             io::Json* result, io::Json* error);
// Calendar-local conversion uses the system C++ time-zone database only in an
// isolated worker. Explicit offsets need no database. No process TZ mutation.
ObjectReply ProviderCalendarTime(std::string_view value, std::string_view zone,
                                 std::string* utc);
ObjectReply GoogleCalendarProvider(std::string_view method,
                                   const io::Json& params,
                                   const ProviderFetch& fetch,
                                   io::Json* result);
ObjectReply GoogleDriveProvider(std::string_view method, const io::Json& params,
                                const ProviderFetch& fetch, io::Json* result);
ObjectReply GmailProvider(std::string_view method, const io::Json& params,
                          const ProviderFetch& fetch, io::Json* result);
}  // namespace q4t::server::mcp_detail
