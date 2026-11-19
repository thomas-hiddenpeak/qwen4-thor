#pragma once

#include "q4t/server/realtime_conversation_session.h"
#include "q4t/server/realtime_session_pump.h"

namespace q4t::server {
class RealtimeConversationPump final : public RealtimeSessionPump {
 public:
  RealtimeConversationPump(std::string id, std::string conversation_id,
                           io::Json settings, WebSocketOutput& output,
                           RealtimeConversationCallbacks callbacks,
                           size_t message_bytes = kWebSocketMessageLimit);
};
}  // namespace q4t::server
