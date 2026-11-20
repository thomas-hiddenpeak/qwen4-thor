#pragma once

#include "q4t/server/realtime_conversation.h"
#include "q4t/server/responses_mcp_state.h"

namespace q4t::server {
// Pure validation preview, never permission to execute. Converts lifecycle
// fields/errors for the native model adapter while keeping positions intact.
ObjectReply PreviewRealtimeMcpHistory(const io::Json& items,
                                      io::Json* projected);
// Run on the generation worker before model admission. Exact session-owned
// snapshots already carry the native owner's result; client-provided copies
// must match sealed evidence in the same authenticated scope. No remote calls.
// A failed optional import or a local cancellation remains local history and
// does not require that the failed auxiliary operation created durable state.
ObjectReply ResolveRealtimeMcpHistory(
    ObjectStore* objects, const ChatPlanConfig& config,
    const RealtimeConversationSnapshot& snapshot, const StateAccess& access,
    io::Json* projected);
}  // namespace q4t::server
