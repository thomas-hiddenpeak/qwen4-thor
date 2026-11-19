#include "q4t/server/realtime_transcription_pump.h"

#include <algorithm>

namespace q4t::server {
RealtimeTranscriptionPump::RealtimeTranscriptionPump(
    std::string id, WebSocketOutput& output,
    RealtimeTranscriptionCallbacks callbacks, uint32_t max_audio_seconds,
    size_t message_bytes)
    : RealtimeSessionPump(message_bytes) {
  const auto original = std::move(callbacks.interrupted);
  callbacks.interrupted = [stop = StopSignal(), original] {
    return stop() || (original && original());
  };
  Attach(std::make_shared<RealtimeTranscriptionSession>(
      std::move(id), output, std::move(callbacks), max_audio_seconds,
      message_bytes));
}
}  // namespace q4t::server
