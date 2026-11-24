// WebRTC media and optional SCTP controls for a native translation session.
#pragma once

#include "q4t/server/live_media_call.h"
#include "q4t/server/realtime_translation_session.h"

namespace q4t::server {
class TranslationMediaCall final : public IMediaCall {
 public:
  TranslationMediaCall(RealtimeTranslationNative native, uint32_t max_seconds,
                       size_t message_bytes, Send send, bool expect_channel);
  ~TranslationMediaCall() override;
  ObjectReply Start(const io::Json& configuration) override;
  bool Receive(char type, std::string_view payload) override;
  void CloseDataChannel() override;
  bool Tick(Clock::time_point now) override;
  bool Finished() const override;
  bool Succeeded() const override { return !session_->Failed(); }
  bool EndTransport() override;

 private:
  struct Input {
    bool audio;
    std::string bytes;
  };
  bool Buffer(bool audio, std::string_view payload);
  bool Submit();
  bool Publish(io::Json event);

  const Send send_;
  const size_t message_bytes_, batch_bytes_;
  std::deque<Input> input_;
  size_t input_bytes_ = 0;
  Clock::time_point batch_started_{}, blocked_since_{}, expires_{};
  bool verified_ = false;
  // The worker produces both audio and control events. Only the carrier
  // thread writes packets. Cancellation wakes producers waiting for playout.
  mutable std::mutex output_mutex_;
  std::condition_variable writable_;
  MediaDataChannel channel_;
  MediaPcmPlayout playout_;
  std::string closed_event_;
  bool ended_ = false;
  std::shared_ptr<RealtimeTranslationSession> session_;
  RealtimeTranslationPump pump_;
};
}  // namespace q4t::server
