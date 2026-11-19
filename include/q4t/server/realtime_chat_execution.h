// One Realtime response invokes the normal authenticated Chat owner once.
#pragma once

#include "q4t/server/client_policy.h"
#include "q4t/server/native_chat_generation.h"
#include "q4t/server/realtime_conversation_session.h"
#include "q4t/server/http_request.h"

namespace q4t::server {
RealtimeGenerationResult ExecuteRealtimeChat(
    const io::Json& request, const ClientIdentity& client, ClientLease& permit,
    const std::shared_ptr<RequestControl>& control,
    const RealtimeDeltaSink& emit,
    const std::function<void(NativeChatGeneration&)>& execute,
    HttpBodyBudget& input_budget);
}  // namespace q4t::server
