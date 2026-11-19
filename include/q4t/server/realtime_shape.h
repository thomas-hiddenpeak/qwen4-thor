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
}  // namespace q4t::server
