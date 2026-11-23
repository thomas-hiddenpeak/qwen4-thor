// Shared ordered work queue for native transcription and conversation sessions.
#pragma once

#include <atomic>
#include <condition_variable>
#include <deque>
#include <functional>
#include <memory>
#include <mutex>
#include <thread>

#include "q4t/server/websocket.h"
#include "q4t/server/object_store.h"

namespace q4t::server {
inline constexpr size_t kRealtimeInboxBytes = 16ULL << 20;
inline constexpr size_t kRealtimeInboxMessages = 8;
class RealtimeSessionPump {
 public:
  // One socket owner starts/enqueues/stops/joins. A distinct worker owns all
  // session mutations and model joins. No parsing or backend work on the
  // reader.
  bool Start();
  ObjectReply Start(const io::Json* configuration);
  bool Message(WebSocketOpcode opcode, std::string_view message);
  bool CanAccept(size_t message_bytes) const;
  bool Alive() const { return started_ && !stopped_.load(); }
  void Stop() noexcept;
  void Join() noexcept;
  size_t InputBytes() const;
  size_t InputMessages() const;

 protected:
  explicit RealtimeSessionPump(size_t message_bytes)
      : message_bytes_(message_bytes) {}
  ~RealtimeSessionPump();
  auto StopSignal() const {
    return [this] { return stopped_.load(); };
  }
  // The closures own the session until the pump has stopped and joined. The
  // session's interruption callback may reference this base's live stop flag.
  template <typename Session>
  void Attach(std::shared_ptr<Session> session) {
    operations_.start = [session](const io::Json* settings) -> ObjectReply {
      if (settings) {
        if constexpr (requires { session->StartConfigured(*settings); }) {
          return session->StartConfigured(*settings);
        }
      } else if (session->Start()) {
        return {};
      }
      return {.value={}, .http_status=503, .code="realtime_session_unavailable",
              .message="Realtime session initialization failed"};
    };
    operations_.message = [session](auto opcode, auto text) {
      return session->Message(opcode, text);
    };
    operations_.tick = [session] { return session->Tick(); };
    operations_.cancel = [session] { session->Cancel(); };
    operations_.join = [session] { session->Join(); };
  }

 private:
  struct MessageItem {
    WebSocketOpcode opcode;
    std::string text;
    size_t charge = 0;
  };
  void Run() noexcept;
  const size_t message_bytes_;
  std::atomic<bool> stopped_{false};
  std::thread worker_;
  mutable std::mutex mutex_;
  std::condition_variable changed_;
  std::deque<MessageItem> messages_;
  size_t bytes_ = 0, count_ = 0;
  bool started_ = false;
  // Destroy captured sessions first, while the pump's state is still alive.
  struct Operations {
    std::function<ObjectReply(const io::Json*)> start;
    std::function<bool()> tick;
    std::function<bool(WebSocketOpcode, std::string_view)> message;
    std::function<void()> cancel, join;
  } operations_;
};
}  // namespace q4t::server
