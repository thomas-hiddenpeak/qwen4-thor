// Connects the existing Realtime conversation to a negotiated WebRTC or SIP
// call.
#pragma once

#include <list>

#include "q4t/server/live_media_call.h"
#include "q4t/server/realtime_conversation_session.h"
#include "realtime_call_pcm.h"

namespace q4t::server {
enum class RealtimeCarrier { kSip, kWebRtcAudio, kWebRtcData };

class RealtimeMediaCall final : public IMediaCall {
 public:
  RealtimeMediaCall(io::Json defaults, RealtimeConversationCallbacks callbacks,
                    size_t message_bytes, Send send,
                    std::shared_ptr<LiveSideband> sideband,
                    RealtimeCarrier carrier = RealtimeCarrier::kSip);
  ObjectReply Start(const io::Json& configuration) override;
  bool Receive(char type, std::string_view payload) override;
  void CloseDataChannel() override { channel_.Disconnect(); }
  bool Tick(Clock::time_point now) override;
  bool EndTransport() override;
  bool Finished() const override { return ended_; }

 private:
  struct Speech {
    std::string id;
    RealtimeCallPcm decoder;
    std::deque<std::string> chunks;
    size_t offset = 0;
    bool started = false, done = false, cleared = false;
    std::string item_id;
    size_t submitted_samples = 0;
    bool in_conversation = false, history_settled = false;
  };
  bool Publish(std::string_view wire);
  void PublishEvent(std::string_view wire, std::string_view session = {});
  bool Input(std::string_view pcm, bool finish = false);
  bool Control(WebSocketOpcode opcode, std::string_view wire);
  bool Playback(Clock::time_point now);
  void BufferEvent(const char* type, const std::string& id);
  bool Clear(bool default_only);
  bool TruncateCleared();
  const Send send_;
  std::shared_ptr<LiveSideband> sideband_;
  const RealtimeCarrier carrier_;
  MediaDataChannel channel_;
  Clock::time_point channel_blocked_{};
  std::optional<RealtimeCallPcm> encoder_;
  RealtimeAudioFormat input_format_;
  std::list<Speech> speech_;
  size_t pcm_bytes_ = 0;
  Clock::time_point next_{}, blocked_{};
  bool verified_ = false, ended_ = false, failed_ = false;
  RealtimeConversationSession session_;
};
}  // namespace q4t::server
