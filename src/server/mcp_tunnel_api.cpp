#include "q4t/server/mcp_tunnel.h"

#include <charconv>
#include <set>

#include "object_protocol.h"
#include "q4t/server/application_query.h"
#include "q4t/server/input_contract.h"

namespace q4t::server {
namespace {
using namespace object_detail;
ObjectReply Invalid() {
  return Error(400, "invalid_tunnel_request", "Invalid tunnel request");
}
bool Timeout(const Json& value, int* timeout) {
  if (!value.IsString() || value.str.empty()) return false;
  int parsed = 0;
  const auto result = std::from_chars(
      value.str.data(), value.str.data() + value.str.size(), parsed);
  if (result.ec != std::errc{} ||
      result.ptr != value.str.data() + value.str.size() || parsed < 0 ||
      parsed > 15000)
    return false;
  *timeout = parsed;
  return true;
}
}  // namespace

ObjectReply HandleMcpTunnelRequest(
    McpTunnelBroker& broker, const BinaryAccess& access,
    std::string_view method, std::string_view target, const std::string& body,
    std::string_view instance, std::string_view shard_token) {
  const auto route = ParseMcpTunnelRoute(target);
  if (route.kind == McpTunnelRouteKind::kInvalid)
    return Error(404, "route_not_found", "Tunnel route not found");
  if (!McpTunnelMethodAllowed(method, target)) {
    auto reply = Error(405, "method_not_allowed", "Method not allowed");
    reply.value =
        Object({{"allow",
                 String(route.kind == McpTunnelRouteKind::kResponse ? "POST"
                                                                    : "GET")}});
    return reply;
  }
  const auto split = target.find('?');
  auto query =
      split == target.npos ? std::string_view{} : target.substr(split + 1);
  if (route.kind != McpTunnelRouteKind::kPoll && !query.empty())
    return Invalid();
  if (method == "GET" && (!body.empty() || !shard_token.empty()))
    return Invalid();
  if (route.kind == McpTunnelRouteKind::kMetadata)
    return broker.Describe(access, route.id);
  if (route.kind == McpTunnelRouteKind::kResponse) {
    if (body.size() > (4U << 20))
      return Error(413, "mcp_tunnel_capacity",
                   "Tunnel response exceeds capacity");
    Json response;
    if (!io::ParseJson(body, &response, true,
                       {.max_depth = 96,
                        .max_values = 65536,
                        .max_string_bytes = 4U << 20,
                        .reject_duplicate_keys = true,
                        .allow_exact_number_text = true}) ||
        !response.IsObject())
      return Error(400, "invalid_json_payload", "Invalid tunnel response JSON");
    return broker.Respond(access, route.id, instance, shard_token, response);
  }
  int timeout = 15000;
  size_t limit = 25;
  std::vector<std::string> channels;
  std::set<std::string> seen;
  if (query.size() > 8192) return Invalid();
  while (!query.empty()) {
    const auto end = query.find('&');
    auto parsed = ParseApplicationQuery(query.substr(0, end));
    if (!parsed.ok() || parsed.value.object.size() != 1) return Invalid();
    const auto& [key, value] = parsed.value.object[0];
    if (key == "channel") {
      if (!value.IsString() || channels.size() == 16) return Invalid();
      channels.push_back(value.str);
    } else {
      if (!seen.insert(key).second) return Invalid();
      if (key == "limit") {
        if (!IsInputInteger(value) || value.number < 1 || value.number > 25)
          return Invalid();
        limit = value.AsInt();
      } else if (key == "timeout_ms") {
        if (!Timeout(value, &timeout)) return Invalid();
      } else {
        return Invalid();
      }
    }
    if (end == query.npos) break;
    query.remove_prefix(end + 1);
    if (query.empty()) return Invalid();
  }
  return broker.Poll(access, route.id, instance, channels, limit, timeout);
}
}  // namespace q4t::server
