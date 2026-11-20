// SDK projection of owned native MCP calls; not stored-state authentication.
#pragma once

#include "q4t/server/chat_plan.h"

namespace q4t::server {
struct RealtimeMcpCallProjection {
  enum class Phase { kPending, kCompleted, kFailed };
  io::Json item;
  Phase phase = Phase::kPending;
};
// Native Responses calls carry status and different error discriminators.
// Realtime carries their lifecycle in separate events. Preserve actual output
// and structured tool errors, without adding unsupported item fields or
// inventing a successful result. Failure leaves the caller's output intact.
Status ProjectRealtimeMcpCall(const io::Json& native,
                              RealtimeMcpCallProjection* projection,
                              ChatPlanError* error);
}  // namespace q4t::server
