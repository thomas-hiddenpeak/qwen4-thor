#include "q4t/server/realtime_shape.h"

#include <algorithm>

#include "q4t/server/input_contract.h"
#include "q4t/server/realtime_conversation.h"
#include "sip_control_contract.h"

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
Status ValidateWireShape(const io::Json& value, RealtimeShape shape,
                         ChatPlanError* error, std::string_view path) {
  auto status = ValidateRealtimeShape(value, shape, error, path);
  if (status) return status;
  if (shape == RealtimeShape::kClientEvent &&
      value.GetString("type") == "response.create") {
    size_t storage = 0;
    if (!MeasureRealtimeStorage(value, 16ULL << 20, &storage)) return status;
    auto envelope = value;
    auto* response = UniqueField(&envelope, "response");
    if (!response) return status;
    // SDK 7.47.0 exports the GA WithReferencesParam shape separately from
    // its older RealtimeClientEvent union. Validate the full response using
    // that exact pinned root, then the unchanged outer envelope. Do not drop
    // unrecognized response fields or grant references new-item authority.
    status = ValidateRealtimeShape(*response, RealtimeShape::kResponseCreate,
                                   error, std::string(path) + ".response");
    if (!status) return status;
    *response = io::Json{};
    response->type = io::Json::Type::kObject;
    return ValidateRealtimeShape(envelope, shape, error, path);
  }
  const bool flat_session = shape == RealtimeShape::kSession ||
                            shape == RealtimeShape::kCallAccept;
  const bool secret = shape == RealtimeShape::kClientSecretCreate ||
                      shape == RealtimeShape::kClientSecretCreated;
  if (!flat_session && !secret &&
      shape != RealtimeShape::kClientEvent &&
      shape != RealtimeShape::kServerEvent)
    return status;
  if (!flat_session && !secret) {
    const auto type = value.GetString("type");
    if (type != "session.update" && type != "session.created" &&
        type != "session.updated")
      return status;
  }
  size_t storage = 0;
  if (!MeasureRealtimeStorage(value, 16ULL << 20, &storage)) return status;
  auto projected = value;
  auto* session = flat_session ? &projected : UniqueField(&projected, "session");
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
}  // namespace
Status ValidateRealtimeWireShape(const io::Json& value, RealtimeShape shape,
                                 ChatPlanError* error, std::string_view path) {
  if (shape == RealtimeShape::kCallReject || shape == RealtimeShape::kCallRefer) {
    // calls.reject accepts CallRejectParams | null | undefined.
    if (shape == RealtimeShape::kCallReject && value.IsNull()) return {};
    auto status = ValidateRealtimeShape(value, shape, error, path);
    if (!status) return status;
    return shape == RealtimeShape::kCallReject
               ? CheckSipRejectCode(value, error)
               : CheckSipReferTarget(value, error);
  }
  if (shape == RealtimeShape::kClientSecretCreate) {
    auto status = ValidateWireShape(value, shape, error, path);
    if (!status) return status;
    const auto* expiry = value.Find("expires_after");
    const auto* seconds = expiry ? expiry->Find("seconds") : nullptr;
    if (seconds && (!IsInputInteger(*seconds) || seconds->number < 10 ||
                    seconds->number > 7200)) {
      if (error)
        *error = {"invalid_request", std::string(path) + ".expires_after.seconds", 400};
      return Status::Fail("Client secret lifetime must be 10 to 7200 seconds");
    }
    return {};
  }
  const auto type = value.GetString("type");
  if (shape != RealtimeShape::kServerEvent ||
      (type != "session.created" && type != "session.updated"))
    return ValidateWireShape(value, shape, error, path);
  size_t storage = 0;
  if (!MeasureRealtimeStorage(value, 16ULL << 20, &storage)) {
    if (error)
      *error = {"item_validation_limit_exceeded", std::string(path), 503};
    return Status::Fail("Session event exceeds validation storage limit");
  }
  auto projected = value;
  auto* session = UniqueField(&projected, "session");
  const auto* id = UniqueField(session, "id");
  const auto* object = UniqueField(session, "object");
  const auto session_type = session ? session->GetString("type") : "";
  const auto expected = session_type == "realtime"
                            ? "realtime.session"
                            : "realtime.transcription_session";
  if (!id || !id->IsString() || !id->str.starts_with("sess_") ||
      !ValidRequestId(id->str) || !object || !object->IsString() ||
      object->str != expected ||
      (session_type != "realtime" && session_type != "transcription")) {
    if (error)
      *error = {"invalid_request", std::string(path) + ".session", 400};
    return Status::Fail("Session output requires its native id and object");
  }
  // The official GA server-event reference requires these response fields.
  // SDK 7.47.0 instead types both events as the *request* configuration.
  // Validate ownership fields above, then project only those two fields for
  // its unchanged type oracle. Client settings never enter this projection.
  std::erase_if(session->object, [](const auto& entry) {
    return entry.first == "id" || entry.first == "object";
  });
  return ValidateWireShape(projected, shape, error, path);
}
}  // namespace q4t::server
