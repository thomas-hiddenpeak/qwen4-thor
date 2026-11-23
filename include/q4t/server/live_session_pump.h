#pragma once

#include "q4t/server/live_session.h"
#include "q4t/server/realtime_session_pump.h"

namespace q4t::server {
class LiveSessionPump final : public RealtimeSessionPump {
 public:
  LiveSessionPump(std::string id, uint64_t expires_at, WebSocketOutput& output,
                  LiveSessionCallbacks callbacks,
                  LiveSessionLimits limits = {});
  bool Finalized() const { return session_->Finalized(); }

 private:
  std::shared_ptr<LiveSession> session_;
};
}  // namespace q4t::server
