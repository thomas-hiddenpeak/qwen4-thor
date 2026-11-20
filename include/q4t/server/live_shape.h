// Pinned Live wire shapes; resource, authority and execution checks are
// separate.
#pragma once

#include "q4t/server/chat_plan.h"

namespace q4t::server {
enum class LiveShape {
  kClientEvent,
  kServerEvent,
  kSessionStart,
  kSessionUpdate,
  kSession,
  kMediaCreate,
  kMediaCreated,
  kSidebandClient,
  kSidebandServer,
  kForkClient,
  kForkServer,
  kSessionAccept,
  kSessionFork,
  kSessionForked,
  kSessionRefer,
  kSessionReject
};
Status ValidateLiveShape(const io::Json& value, LiveShape shape,
                         ChatPlanError* error, std::string_view path = "event");
}  // namespace q4t::server
