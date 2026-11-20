#include "q4t/server/live_session_pump.h"

namespace q4t::server {
LiveSessionPump::LiveSessionPump(std::string id, uint64_t expires_at,
                                 WebSocketOutput& output,
                                 LiveSessionCallbacks callbacks,
                                 LiveSessionLimits limits)
    : RealtimeSessionPump(limits.message_bytes) {
  const auto original = std::move(callbacks.interrupted);
  callbacks.interrupted = [stop = StopSignal(), original] {
    return stop() || (original && original());
  };
  session_ = std::make_shared<LiveSession>(std::move(id), expires_at, output,
                                           std::move(callbacks), limits);
  Attach(session_);
}
}  // namespace q4t::server
