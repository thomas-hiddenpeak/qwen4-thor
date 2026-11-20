#include "q4t/server/realtime_mcp_history.h"

#include <algorithm>

#include "object_protocol.h"
#include "q4t/server/realtime_mcp_item.h"
#include "q4t/server/realtime_shape.h"

namespace q4t::server {
namespace {
using namespace object_detail;
bool Accepted(const ObjectReply& reply) {
  return reply.ok() && !reply.commit_unknown && reply.http_status >= 200 &&
         reply.http_status < 300;
}
ObjectReply Invalid(std::string_view message) {
  return Error(400, "realtime_mcp_history_invalid", std::string(message));
}
ObjectReply Active(const StateAccess& access) {
  if (access.cancelled && access.cancelled())
    return Error(499, "request_cancelled", "MCP history lookup cancelled");
  if (std::chrono::steady_clock::now() >= access.deadline)
    return Error(408, "request_timeout", "MCP history deadline expired");
  return {};
}
bool Matches(const Json& source, const Json& projected) {
  for (const auto& [key, value] : source.object) {
    if (key == "object") continue;
    const auto* expected = projected.Find(key);
    if (!expected && value.IsNull()) continue;
    if (!expected || OutputJson(value) != OutputJson(*expected)) return false;
  }
  return true;
}
}  // namespace

ObjectReply PreviewRealtimeMcpHistory(const Json& items, Json* projected) {
  size_t bytes = 0;
  if (!projected || !items.IsArray() || items.array.size() > 128 ||
      !MeasureRealtimeStorage(items, 16ULL << 20, &bytes))
    return Invalid("MCP history exceeds its input limit");
  auto next = items;
  ChatPlanError error;
  for (auto& item : next.array) {
    if (!item.GetString("type").starts_with("mcp_")) continue;
    if (!ValidateRealtimeShape(item, RealtimeShape::kItem, &error))
      return Invalid("Invalid Realtime MCP history shape");
    std::erase_if(item.object,
                  [](const auto& field) { return field.first == "object"; });
    if (item.GetString("type") != "mcp_call") continue;
    const auto* failure = item.Find("error");
    const auto* output = item.Find("output");
    const bool failed = failure && !failure->IsNull();
    Set(&item, "status",
        String(failed                         ? "failed"
               : output && output->IsString() ? "completed"
                                              : "in_progress"));
    if (failed) {
      auto mapped = *item.Find("error");
      if (mapped.GetString("type") == "protocol_error") {
        Set(&mapped, "type", String("mcp_protocol_error"));
      } else if (mapped.GetString("type") == "tool_execution_error") {
        mapped = Object({{"type", String("mcp_tool_execution_error")},
                         {"content", *mapped.Find("message")}});
      }
      Set(&item, "error", std::move(mapped));
      Json arguments;
      if (!io::ParseJson(item.GetString("arguments"), &arguments, true) ||
          !arguments.IsObject())
        Set(&item, "status", String("incomplete"));
    }
  }
  if (!MeasureRealtimeStorage(next, 32ULL << 20, &bytes))
    return Invalid("Native MCP history exceeds its projection limit");
  *projected = std::move(next);
  return {};
}

ObjectReply ResolveRealtimeMcpHistory(
    ObjectStore* objects, const ChatPlanConfig& config,
    const RealtimeConversationSnapshot& snapshot, const StateAccess& access,
    Json* projected) {
  if (!projected || (!snapshot.owned_mcp.empty() &&
                     snapshot.owned_mcp.size() != snapshot.items.array.size()))
    return Invalid("Invalid native MCP history provenance");
  auto reply = Active(access);
  if (!Accepted(reply)) return reply;
  Json next;
  reply = PreviewRealtimeMcpHistory(snapshot.items, &next);
  if (!Accepted(reply)) return reply;
  for (size_t i = 0; i < next.array.size(); ++i) {
    const auto& source = snapshot.items.array[i];
    if (!source.GetString("type").starts_with("mcp_") ||
        (!snapshot.owned_mcp.empty() && snapshot.owned_mcp[i]))
      continue;
    if (!objects || access.scope.empty())
      return Error(503, "mcp_state_unavailable",
                   "MCP history storage unavailable");
    auto candidate = next.array[i];
    const bool call = candidate.GetString("type") == "mcp_call";
    if (call) {
      // Recover the actual terminal through its identity first. Realtime's
      // error spelling differs; compare against the exact SDK projection below.
      std::erase_if(candidate.object, [](const auto& field) {
        return field.first == "output" || field.first == "error";
      });
      Set(&candidate, "status", String("in_progress"));
    }
    reply = ResponsesMcpState(*objects, config).Authenticate(candidate, access);
    if (!Accepted(reply)) return reply;
    if (call) {
      RealtimeMcpCallProjection view;
      ChatPlanError error;
      if (!ProjectRealtimeMcpCall(reply.value, &view, &error) ||
          !Matches(source, view.item))
        return Invalid("MCP result differs from its authenticated history");
    }
    next.array[i] = std::move(reply.value);
    reply = Active(access);
    if (!Accepted(reply)) return reply;
  }
  size_t bytes = 0;
  if (!MeasureRealtimeStorage(next, 32ULL << 20, &bytes))
    return Invalid("Authenticated MCP history exceeds its projection limit");
  *projected = std::move(next);
  return {};
}
}  // namespace q4t::server
