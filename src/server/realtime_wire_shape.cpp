#include "q4t/server/realtime_shape.h"

#include <algorithm>

#include "q4t/server/realtime_conversation.h"

namespace q4t::server {
namespace {
io::Json* UniqueField(io::Json* object, std::string_view name) {
  if (!object || !object->IsObject()) return nullptr;
  io::Json* found = nullptr;
  for (auto& [key, value] : object->object) {
    if (key != name) continue;
    if (found) return nullptr;  // Projection must not hide duplicate fields.
    found = &value;
  }
  return found;
}
}  // namespace
Status ValidateRealtimeWireShape(const io::Json& value, RealtimeShape shape,
                                 ChatPlanError* error, std::string_view path) {
  auto status = ValidateRealtimeShape(value, shape, error, path);
  if (status) return status;
  if (shape != RealtimeShape::kSession &&
      shape != RealtimeShape::kClientEvent &&
      shape != RealtimeShape::kServerEvent)
    return status;
  if (shape != RealtimeShape::kSession) {
    const auto type = value.GetString("type");
    if (type != "session.update" && type != "session.created" &&
        type != "session.updated")
      return status;
  }
  size_t storage = 0;
  if (!MeasureRealtimeStorage(value, 16ULL << 20, &storage)) return status;
  auto projected = value;
  auto* session = shape == RealtimeShape::kSession
                      ? &projected
                      : UniqueField(&projected, "session");
  if (!session || (session->GetString("type") != "realtime" &&
                   session->GetString("type") != "transcription"))
    return status;
  auto* input = UniqueField(UniqueField(session, "audio"), "input");
  if (!input) return status;
  // SDK 7.47.0's declarations omit null although their own comments and the
  // official GA client/server-event reference explicitly allow these clears:
  // https://developers.openai.com/api/reference/resources/realtime/client-events
  // https://developers.openai.com/api/reference/resources/realtime/server-events
  // Only the validation projection omits a documented null; the real update
  // and acknowledgement retain it. All other fields still use the exact graph.
  bool changed = false;
  for (const auto* key : {"noise_reduction", "transcription"}) {
    const auto* field = UniqueField(input, key);
    if (!field || !field->IsNull()) continue;
    std::erase_if(input->object,
                  [&](const auto& entry) { return entry.first == key; });
    changed = true;
  }
  return changed ? ValidateRealtimeShape(projected, shape, error, path)
                 : status;
}
}  // namespace q4t::server
