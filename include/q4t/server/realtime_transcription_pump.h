// Ordered, bounded session work outside the socket reader/writer thread.
#pragma once

#include "q4t/server/realtime_transcription_session.h"

namespace q4t::server {
inline constexpr size_t kRealtimeInboxBytes = 16ULL << 20;
inline constexpr size_t kRealtimeInboxMessages = 8;

class RealtimeTranscriptionPump {
 public:
  RealtimeTranscriptionPump(std::string id, WebSocketOutput& output,
                            RealtimeTranscriptionCallbacks callbacks,
                            uint32_t max_audio_seconds,
                            size_t message_bytes = kWebSocketMessageLimit);
  ~RealtimeTranscriptionPump();
  // Start/Message/Stop/Join belong to one socket owner; the worker owns every
  // subsequent session mutation. Stop signals, never concurrently cancels the
  // session's queues or joins a model borrower on the socket thread.
  bool Start();
  bool Message(WebSocketOpcode opcode, std::string_view message);
  bool CanAccept(size_t message_bytes) const;
  bool Alive() const { return started_ && !stopped_.load(); }
  void Stop() noexcept;
  void Join() noexcept;
  size_t InputBytes() const;
  size_t InputMessages() const;

 private:
  struct MessageItem {
    WebSocketOpcode opcode;
    std::string text;
    size_t charge = 0;
  };
  void Run() noexcept;
  const size_t message_bytes_;
  std::atomic<bool> stopped_{false};
  std::unique_ptr<RealtimeTranscriptionSession> session_;
  std::thread worker_;
  mutable std::mutex mutex_;
  std::condition_variable changed_;
  std::deque<MessageItem> messages_;
  size_t bytes_ = 0, count_ = 0;
  bool started_ = false;
};
}  // namespace q4t::server
