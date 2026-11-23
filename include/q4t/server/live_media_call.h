// One owner maps a negotiated WebRTC carrier to the native Live session.
#pragma once

#include "q4t/server/live_media_policy.h"
#include "q4t/server/live_session.h"
#include "q4t/server/live_sideband.h"

namespace q4t::server {
class IMediaCall {
 public:
  using Clock = std::chrono::steady_clock;
  using Send = std::function<bool(char, std::string_view)>;
  virtual ~IMediaCall() = default;
  virtual ObjectReply Start(const io::Json& configuration) = 0;
  virtual bool Receive(char type, std::string_view payload) = 0;
  virtual bool Tick(Clock::time_point now) = 0;
  virtual bool Finished() const = 0;
  virtual bool EndTransport() = 0;
};

class LiveMediaCall final : public IMediaCall {
 public:
  LiveMediaCall(std::string id, uint64_t expires_at, bool expect_channel,
                LiveSessionCallbacks callbacks, LiveSessionLimits limits,
                Send send, std::shared_ptr<LiveSideband> sideband = {});
  LiveMediaCall(const LiveMediaCall&) = delete;
  LiveMediaCall& operator=(const LiveMediaCall&) = delete;
  ObjectReply Start(const io::Json& configuration) override;
  bool Receive(char type, std::string_view payload) override;
  bool Tick(Clock::time_point now) override;
  bool Finished() const override;
  bool EndTransport() override;

 private:
  bool Publish(std::string_view wire);
  void DropData();
  bool close_pending_ = false, transport_ended_ = false;
  LiveMediaPolicy frontend_;
  const Send send_;
  std::shared_ptr<LiveSideband> sideband_;
  bool expect_channel_, verified_ = false, opened_ = false, closed_ = false;
  bool failed_ = false;
  size_t pcm_offset_ = 0, queued_pcm_ = 0, queued_data_ = 0;
  std::deque<std::string> media_, data_;
  Clock::time_point next_audio_{}, blocked_since_{}, waiting_data_{};
  // Destroy the backend while the channel, media queues and callbacks exist.
  LiveSession session_;
};
}  // namespace q4t::server
