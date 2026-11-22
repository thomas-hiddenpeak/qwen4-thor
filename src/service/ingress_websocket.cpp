#include "q4t/service/ingress_websocket.h"

#include <poll.h>
#include <sys/socket.h>

#include <algorithm>
#include <array>
#include <cerrno>

#include "q4t/server/http_framing.h"
#include "q4t/service/ingress_duplex.h"

namespace q4t::service::ingress {
namespace {
using namespace std::chrono_literals;
bool Response(std::string_view raw, std::string_view expected_accept,
               std::string_view selected_protocol) {
  const auto first = raw.find("\r\n");
  if (first == raw.npos ||
      !raw.substr(0, first).starts_with("HTTP/1.1 101 ")) return false;
  for (unsigned char c : raw.substr(0, first))
    if (c < 32 || c > 126) return false;
  raw.remove_prefix(first + 2);
  bool accept = false, upgrade = false, protocol = false;
  std::vector<std::string> connection;
  while (!raw.empty()) {
    const auto end = raw.find("\r\n");
    std::string name;
    std::string_view value;
    if (end == raw.npos ||
        !server::http::Field(raw.substr(0, end), &name, &value)) return false;
    if (name == "sec-websocket-accept") {
      if (accept || value != expected_accept) return false;
      accept = true;
    } else if (name == "upgrade") {
      if (upgrade || server::http::Lower(value) != "websocket") return false;
      upgrade = true;
    } else if (name == "connection") {
      if (!server::http::Tokens(value, &connection)) return false;
    } else if (name == "sec-websocket-protocol") {
      if (protocol || selected_protocol.empty() || value != selected_protocol)
        return false;
      protocol = true;
    } else if (name == "sec-websocket-extensions" || name == "content-length" ||
               name == "transfer-encoding") {
      return false;
    }
    raw.remove_prefix(end + 2);
  }
  bool switching = false;
  for (const auto& option : connection) {
    if (option == "close" || server::http::Protected(option)) return false;
    switching |= option == "upgrade";
  }
  return accept && upgrade && switching &&
         (protocol || selected_protocol.empty());
}
}  // namespace

bool WebSocketCredentials(server::HttpRequestHeaders* headers) {
  constexpr std::string_view prefix = "openai-insecure-api-key.";
  std::string_view key;
  for (const auto& protocol : headers->websocket_protocols) {
    if (!protocol.starts_with(prefix)) continue;
    const auto value = std::string_view(protocol).substr(prefix.size());
    if (!key.empty() || !headers->authorization.empty() || value.empty() ||
        value.size() > 128) return false;
    key = value;
  }
  if (!key.empty()) headers->authorization = "Bearer " + std::string(key);
  return true;
}

void RelayWebSocket(int upstream, Connection& client,
                     std::string_view expected_accept,
                     std::string_view selected_protocol,
                     std::string_view client_prefix, int idle_seconds,
                     std::string_view request_id, bool* started,
                     const std::function<bool()>& authorized) {
  client.keep_alive = false;
  const auto reject = [&](std::string_view code) {
    Error(client, 503, code, request_id, started);
  };
  std::string response;
  const auto deadline = Clock::now() + std::chrono::seconds(
      std::clamp(idle_seconds, 1, 15));
  size_t end;
  while ((end = response.find("\r\n\r\n")) == response.npos) {
    if (stopped.load() || Clock::now() >= deadline ||
        response.size() >= 65536 || client.Cancelled()) {
      reject("service_upgrade_unavailable");
      return;
    }
    pollfd event{upstream, POLLIN, 0};
    const int ready = ::poll(&event, 1, 100);
    if (ready < 0 && errno == EINTR) continue;
    if (ready < 0 || (event.revents & POLLNVAL)) {
      reject("service_upgrade_unavailable");
      return;
    }
    if (!ready) continue;
    std::array<char, 16384> buffer;
    const auto count = ::recv(upstream, buffer.data(),
        std::min(buffer.size(), 65536 - response.size()), MSG_DONTWAIT);
    if (count < 0 && (errno == EAGAIN || errno == EINTR)) continue;
    if (count <= 0) {
      reject("service_upgrade_unavailable");
      return;
    }
    response.append(buffer.data(), count);
  }
  if (!authorized()) {
    reject("service_upgrade_revoked");
    return;
  }
  if (!response.starts_with("HTTP/1.1 101 ")) {
    Relay(upstream, client, std::clamp(idle_seconds, 1, 15), request_id,
           started, true, response);
    return;
  }
  if (!Response(std::string_view(response).substr(0, end + 2),
                 expected_accept, selected_protocol)) {
    reject("service_upgrade_invalid");
    return;
  }
  // Do not pass arbitrary upstream headers, extensions, cookies or credentials.
  const std::string head =
      "HTTP/1.1 101 Switching Protocols\r\nConnection: Upgrade\r\n"
      "Upgrade: websocket\r\nSec-WebSocket-Accept: " +
      std::string(expected_accept) + "\r\n" +
      (selected_protocol.empty() ? "" : "Sec-WebSocket-Protocol: " +
                                        std::string(selected_protocol) + "\r\n") +
      "X-Request-Id: " + std::string(request_id) + "\r\n" +
      client.extra_headers + "\r\n";
  if (!client.Send(head, Clock::now() + 5s, started)) return;
  DuplexLimits limits;
  limits.idle = std::chrono::seconds(std::clamp(idle_seconds, 1, 3600));
  RelayDuplex(upstream, client, client_prefix,
                std::string_view(response).substr(end + 4), limits, authorized);
}
}  // namespace q4t::service::ingress
