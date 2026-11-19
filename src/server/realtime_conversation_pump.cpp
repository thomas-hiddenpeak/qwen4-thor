#include "q4t/server/realtime_conversation_pump.h"

namespace q4t::server {
RealtimeConversationPump::RealtimeConversationPump(
    std::string id, std::string conversation_id, io::Json settings,
    WebSocketOutput& output, RealtimeConversationCallbacks callbacks,
    size_t message_bytes)
    : RealtimeSessionPump(message_bytes) {
  const auto original = std::move(callbacks.interrupted);
  callbacks.interrupted = [stop = StopSignal(), original] {
    return stop() || (original && original());
  };
  Attach(std::make_shared<RealtimeConversationSession>(
      std::move(id), std::move(conversation_id), std::move(settings), output,
      std::move(callbacks), message_bytes));
}
}  // namespace q4t::server
