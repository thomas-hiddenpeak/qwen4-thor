#include "q4t/server/realtime_trace.h"

namespace q4t::server {
namespace {
using io::Json;
std::string_view Text(const Json& value, const char* key) {
  const auto* child = value.Find(key);
  return child && child->IsString() ? std::string_view(child->str)
                                    : std::string_view{};
}
std::string Part(const Json& event) {
  const auto id = Text(event, "item_id");
  const auto* index = event.Find("content_index");
  if (id.empty() || id.size() > 128 || !index || !index->IsNumber() ||
      index->AsInt(-1) < 0 || index->AsInt(-1) > 255)
    return {};
  return std::string(id) + "_part_" + std::to_string(index->AsInt());
}
bool Observe(RealtimeTraceCollector& trace, const Json& event,
             RealtimeTraceTime now) {
  const auto type = Text(event, "type");
  // No delta content, audio bytes, arguments or results are copied.
  if (type.ends_with(".delta")) return false;
  if (type == "response.created" || type == "response.done") {
    const auto* response = event.Find("response");
    if (!response || !response->IsObject()) return false;
    const auto id = Text(*response, "id");
    if (type == "response.created") return trace.Begin("response", id, {}, now);
    return trace.End("response", id, Text(*response, "status"), now,
                     response->Find("usage"));
  }
  if (type == "mcp_list_tools.in_progress")
    return trace.Begin("mcp_import", Text(event, "item_id"), {}, now);
  if (type == "mcp_list_tools.completed" || type == "mcp_list_tools.failed")
    return trace.End("mcp_import", Text(event, "item_id"),
                     type.ends_with(".failed") ? "failed" : "completed", now);
  if (type == "response.mcp_call.in_progress") {
    const bool changed = trace.Begin("mcp_call", Text(event, "item_id"),
                                     Text(event, "response_id"), now);
    return trace.Phase("mcp_call", Text(event, "item_id"), "dispatched", now) ||
           changed;
  }
  if (type == "response.mcp_call_arguments.done")
    return trace.Phase("mcp_call", Text(event, "item_id"), "arguments_ready",
                       now);
  if (type == "response.mcp_call.failed")
    return trace.End("mcp_call", Text(event, "item_id"), "failed", now);
  if (type == "response.output_item.added" ||
      type == "response.output_item.done") {
    const auto* item = event.Find("item");
    if (!item || !item->IsObject()) return false;
    const auto item_type = Text(*item, "type");
    if (item_type != "mcp_call" && item_type != "function_call") return false;
    const auto kind =
        item_type == "mcp_call" ? "mcp_call" : "function_arguments";
    if (type.ends_with(".added"))
      return trace.Begin(kind, Text(*item, "id"), Text(event, "response_id"),
                         now);
    bool linked = false;
    if (item_type == "mcp_call")
      linked = trace.LinkApproval(Text(*item, "id"),
                                  Text(*item, "approval_request_id"));
    const auto* error = item->Find("error");
    const auto status =
        item_type == "mcp_call"
            ? (error && !error->IsNull() ? "failed" : "completed")
            : Text(*item, "status");
    return trace.End(kind, Text(*item, "id"), status, now) || linked;
  }
  if (type == "conversation.item.done") {
    const auto* item = event.Find("item");
    if (!item || !item->IsObject()) return false;
    if (Text(*item, "type") == "mcp_approval_request")
      return trace.Begin("mcp_approval", Text(*item, "id"), {}, now);
    if (Text(*item, "type") == "mcp_approval_response") {
      const auto* approved = item->Find("approve");
      if (!approved || !approved->IsBool()) return false;
      return trace.End("mcp_approval", Text(*item, "approval_request_id"),
                       approved->boolean ? "completed" : "cancelled", now);
    }
  }
  if (type == "conversation.item.added") {
    const auto* item = event.Find("item");
    if (!item || Text(*item, "role") != "user") return false;
    const auto* parts = item->GetArray("content");
    const auto id = Text(*item, "id");
    if (!parts || id.empty() || id.size() > 128 || parts->array.size() > 255)
      return false;
    bool changed = false;
    for (size_t i = 0; i < parts->array.size(); ++i)
      if (Text(parts->array[i], "type") == "input_audio")
        changed |= trace.Begin("input_transcription",
                               std::string(id) + "_part_" + std::to_string(i),
                               {}, now);
    return changed;
  }
  if (type == "conversation.item.input_audio_transcription.completed" ||
      type == "conversation.item.input_audio_transcription.failed")
    return trace.End("input_transcription", Part(event),
                     type.ends_with(".failed") ? "failed" : "completed", now,
                     event.Find("usage"));
  if (type == "response.content_part.added") {
    const auto* part = event.Find("part");
    if (!part || Text(*part, "type") != "audio") return false;
    return trace.Begin("output_audio", Part(event), Text(event, "response_id"),
                       now);
  }
  if (type == "response.output_audio.done")
    return trace.End("output_audio", Part(event), "completed", now);
  return false;
}
}  // namespace

bool ObserveRealtimeTraceEvent(RealtimeTraceCollector* collector,
                               const Json& event,
                               RealtimeTraceTime now) noexcept {
  if (!collector || !collector->Valid() || !event.IsObject()) return false;
  collector->EventSeen();
  try {
    return Observe(*collector, event, now);
  } catch (...) {
    // Observer allocation never escapes into a healthy conversation owner.
    collector->ObserverError();
    return false;
  }
}
}  // namespace q4t::server
