// Pinned official SDK shapes; resource and execution checks remain separate.
#pragma once

#include "q4t/server/chat_plan.h"

namespace q4t::server {
enum class RealtimeShape {
  kItem,
  kClientEvent,
  kServerEvent,
  kResponseCreate,
  kSession,
  kResponse
};
Status ValidateRealtimeShape(const io::Json& value, RealtimeShape shape,
                             ChatPlanError* error,
                             std::string_view path = "event");
// Documented GA wire semantics can differ from the pinned SDK declaration.
// Preserve the exact SDK oracle above; this checks the narrowly enumerated
// nullable audio settings and the SDK's separately exported response reference
// parameters without modifying the caller's effective values.
Status ValidateRealtimeWireShape(const io::Json& value, RealtimeShape shape,
                                 ChatPlanError* error,
                                 std::string_view path = "event");
}  // namespace q4t::server
