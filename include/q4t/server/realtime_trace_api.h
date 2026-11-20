#pragma once

#include "q4t/server/realtime_trace_store.h"
#include "q4t/server/responses_state.h"

namespace q4t::server {
// Native extension: standard Realtime tracing settings feed inspectable local
// traces. This endpoint does not impersonate the OpenAI-hosted dashboard.
ObjectReply HandleRealtimeTraceRequest(RealtimeTraceStore& store,
                                       std::string_view method,
                                       std::string_view target,
                                       const std::string& body,
                                       const StateAccess& access);
io::Json RealtimeTraceDiagnostics(const RealtimeTraceStore* store);
}  // namespace q4t::server
