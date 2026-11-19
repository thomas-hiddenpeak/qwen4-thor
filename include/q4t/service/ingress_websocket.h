// Native authenticated HTTP-upgrade proxy, no model or application replay.
#pragma once

#include <functional>
#include <string>
#include <string_view>

#include "q4t/server/http_request.h"
#include "q4t/service/ingress_transport.h"

namespace q4t::service::ingress {
// Resolve the browser credential subprotocol into the existing Bearer policy.
// Reject ambiguous header+protocol credentials; do not echo/forward the key.
bool WebSocketCredentials(server::HttpRequestHeaders* headers);
// Only an explicitly offered application protocol is selected. Credentials
// and extension offers never become an upstream or response protocol.
std::string WebSocketProtocol(const server::HttpRequestHeaders& headers,
                              std::string_view path);
// Upstream 101 must match the locally computed challenge and selected protocol
// before any byte is published as upgraded. Finite 4xx/5xx replies keep their
// HTTP framing. Coalesced frame tails remain exact. Always ends this HTTP
// connection; caller owns sockets/leases, so it must not parse another request.
void RelayWebSocket(int upstream, Connection& client,
                     std::string_view expected_accept,
                     std::string_view selected_protocol,
                     std::string_view client_prefix,
                     int idle_seconds, std::string_view request_id,
                     bool* started,
                     const std::function<bool()>& authorized);
}  // namespace q4t::service::ingress
