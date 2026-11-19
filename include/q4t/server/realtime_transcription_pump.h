// Ordered native transcription outside the socket reader/writer thread.
#pragma once

#include "q4t/server/realtime_session_pump.h"
#include "q4t/server/realtime_transcription_session.h"

namespace q4t::server {
class RealtimeTranscriptionPump final : public RealtimeSessionPump {
 public:
  RealtimeTranscriptionPump(std::string id, WebSocketOutput& output,
                            RealtimeTranscriptionCallbacks callbacks,
                            uint32_t max_audio_seconds,
                            size_t message_bytes = kWebSocketMessageLimit);
};
}  // namespace q4t::server
