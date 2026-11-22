// The media owner publishes once. Each trusted attachment has its own cursor.
#pragma once

#include <chrono>
#include <deque>
#include <memory>
#include <mutex>
#include <optional>

#include "q4t/server/http_request.h"

#include "q4t/server/object_store.h"
#include "q4t/server/websocket.h"

namespace q4t::server {
class LiveSideband {
 public:
  explicit LiveSideband(std::unique_ptr<HttpBodyLease> memory = {})
      : memory_(std::move(memory)) {}
  using Clock = std::chrono::steady_clock;
  enum class Read { kEvent, kPending, kCompleted, kLost };
  struct Command {
    WebSocketOpcode opcode;
    std::string text;
  };
  static constexpr size_t kReplayBytes = 32ULL << 20;
  static constexpr size_t kCommandBytes = kWebSocketMessageLimit;
  void Publish(std::string_view text, Clock::time_point now) noexcept;
  void OutputAudio(uint64_t begin_ticks, std::string_view pcm) noexcept;
  void InputAudio(std::string_view base64) noexcept;
  void Finish(bool graceful) noexcept;
  ObjectReply Attach(uint64_t* next, Clock::time_point now);
  Read Next(uint64_t next, std::shared_ptr<const std::string>* text);
  bool Enqueue(WebSocketOpcode opcode, std::string_view message);
  bool HasSpace(size_t size) const;
  std::optional<Command> Pop();

 private:
  struct Entry {
    uint64_t sequence;
    Clock::time_point at;
    std::shared_ptr<const std::string> text;
  };
  void DropFront();
  void Expire(Clock::time_point now);
  void Lost(Clock::time_point now) noexcept;
  const std::unique_ptr<HttpBodyLease> memory_;
  mutable std::mutex mutex_;
  std::deque<Entry> events_;
  std::deque<Command> commands_;
  size_t bytes_ = 0, command_bytes_ = 0;
  uint64_t sequence_ = 0;
  Clock::time_point incomplete_until_ = Clock::time_point::min();
  bool finished_ = false, graceful_ = false;
};
}  // namespace q4t::server
