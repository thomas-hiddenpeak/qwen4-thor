// Bounded upgraded-socket ownership, independent of any inference backend.
#pragma once

#include <chrono>
#include <condition_variable>
#include <deque>
#include <functional>
#include <mutex>
#include <string>
#include <string_view>

#include "q4t/server/request_control.h"
#include "q4t/server/websocket.h"

namespace q4t::server {
inline constexpr size_t kWebSocketOutputBytes = 4ULL << 20;
inline constexpr size_t kWebSocketOutputMessageBytes = 1ULL << 20;

// One model producer, one socket consumer. Frames, including a partially sent
// front, remain charged until acknowledgement. No overwrite or implicit replay.
// The owner must stop/join its producer before destroying this mailbox.
class WebSocketOutput {
 public:
  explicit WebSocketOutput(size_t bytes = kWebSocketOutputBytes,
                           size_t frames = 64);
  bool Valid() const { return valid_; }
  bool TryPush(WebSocketOpcode opcode, std::string_view payload);
  // A slow consumer blocks only the producer, never the socket reader. The
  // supplied control must outlive this call; cancellation is polled every 5ms.
  bool Push(WebSocketOpcode opcode, std::string_view payload,
            RequestControl& control);
  size_t Bytes() const;
  size_t PeakBytes() const;
  bool Closed() const;
  // Socket-consumer only. Push preserves this view until Ack/Close. Close can
  // retain a partially written front so a close frame never splits its payload.
  std::string_view Front() const;
  void Acknowledge();
  void Close(bool retain_front = false);

 private:
  bool Prepare(WebSocketOpcode opcode, std::string_view payload,
               std::string* frame) const;
  bool Fits(size_t bytes) const;
  const size_t max_bytes_, max_frames_;
  const bool valid_;
  mutable std::mutex mutex_;
  std::condition_variable changed_;
  std::deque<std::string> frames_;
  size_t bytes_ = 0, peak_ = 0;
  bool closed_ = false;
};

enum class WebSocketSessionEnd {
  kPeerClose,
  kPeerEof,
  kProtocol,
  kIo,
  kWriteDeadline,
  kIdleDeadline,
  kSessionDeadline,
  kRevoked,
  kShutdown,
  kApplication,
  kInvalid
};
struct WebSocketSessionLimits {
  size_t message_bytes = kWebSocketMessageLimit;
  std::chrono::milliseconds idle{60000}, write{15000}, session{3600000};
};
struct WebSocketSessionResult {
  WebSocketSessionEnd end = WebSocketSessionEnd::kInvalid;
  uint16_t close_code = 0;
  uint64_t received_bytes = 0, sent_bytes = 0, messages = 0;
  size_t peak_output_bytes = 0;
};
struct WebSocketSessionCallbacks {
  // All callbacks run on the socket thread and must be bounded/nonblocking.
  // Copy/consume the complete message before returning. False closes with1011.
  std::function<bool(WebSocketOpcode, std::string_view)> message;
  std::function<bool()> authorized;
  std::function<bool()> stopped;
  // Runs exactly once, before abandoning output, including exception paths.
  // Signal cancellation here; the caller owns subsequent worker join/drain.
  std::function<void()> cancel;
  // Service completed work and queue transitions even without new input.
  // False requests the same bounded application close as message failure.
  std::function<bool()> tick = {};
  // Optional receiver backpressure. A complete data message stays owned by
  // the decoder until accepted. Authorization/output/stop remain serviced;
  // a sustained wait has the same bounded duration as an output write.
  std::function<bool(size_t)> message_ready = {};
};

// Borrowed plain backend socket, no fd flag changes and no ownership transfer.
// Native TLS terminates at the separately validated ingress duplex adapter.
// Initial bytes are the HTTP reader's unread tail (<=64KiB). No HTTP writes,
// reconnect, heartbeat thread, model restart or success inferred from TCP EOF.
WebSocketSessionResult RunWebSocketSession(
    int fd, std::string_view initial, WebSocketOutput& output,
    const WebSocketSessionCallbacks& callbacks,
    WebSocketSessionLimits limits = {});
}  // namespace q4t::server
