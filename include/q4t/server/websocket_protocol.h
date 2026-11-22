// Shared selection for both ends of the authenticated WebSocket upgrade.
#pragma once

#include <algorithm>
#include <span>
#include <string>
#include <string_view>

namespace q4t::server {
inline std::string_view SelectWebSocketProtocol(
    std::string_view target, std::span<const std::string> offered) {
  const auto path = target.substr(0, target.find('?'));
  std::string_view name;
  if (path == "/v1/live/sessions") {
    name = "live";
  } else if (path == "/v1/realtime") {
    name = "realtime";
  } else {
    return {};
  }
  return std::ranges::find(offered, name) != offered.end() ? name : "";
}
}  // namespace q4t::server
