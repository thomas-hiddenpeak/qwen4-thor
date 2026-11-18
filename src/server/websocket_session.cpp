#include "q4t/server/websocket_session.h"

#include <poll.h>
#include <sys/socket.h>

#include <algorithm>
#include <array>
#include <cerrno>

namespace q4t::server {
using Clock = std::chrono::steady_clock;
WebSocketOutput::WebSocketOutput(size_t bytes, size_t frames)
    : max_bytes_(bytes),
      max_frames_(frames),
      valid_(bytes >= 128 && bytes <= kWebSocketOutputBytes && frames >= 1 &&
             frames <= 64) {}

bool WebSocketOutput::Prepare(WebSocketOpcode opcode, std::string_view payload,
                              std::string* frame) const {
  // Control frames belong to the socket loop, including the one close owner.
  return valid_ &&
         (opcode == WebSocketOpcode::kText ||
          opcode == WebSocketOpcode::kBinary) &&
         payload.size() <= max_bytes_ &&
         EncodeWebSocketFrame(opcode, payload, std::nullopt, frame,
                              kWebSocketOutputMessageBytes) &&
         frame->capacity() <= max_bytes_;
}
bool WebSocketOutput::Fits(size_t bytes) const {
  return !closed_ && frames_.size() < max_frames_ &&
         bytes <= max_bytes_ - bytes_;
}
bool WebSocketOutput::TryPush(WebSocketOpcode opcode,
                              std::string_view payload) {
  std::string frame;
  if (!Prepare(opcode, payload, &frame)) return false;
  const std::lock_guard lock(mutex_);
  if (!Fits(frame.capacity())) return false;
  const auto bytes = frame.capacity();
  frames_.push_back(std::move(frame));
  bytes_ += bytes;
  peak_ = std::max(peak_, bytes_);
  return true;
}
bool WebSocketOutput::Push(WebSocketOpcode opcode, std::string_view payload,
                           RequestControl& control) {
  std::string frame;
  if (!Prepare(opcode, payload, &frame)) return false;
  std::unique_lock lock(mutex_);
  while (!closed_ && !control.Cancelled() && !Fits(frame.capacity()))
    changed_.wait_for(lock, std::chrono::milliseconds(5));
  if (closed_ || control.Cancelled()) return false;
  const auto bytes = frame.capacity();
  frames_.push_back(std::move(frame));
  bytes_ += bytes;
  peak_ = std::max(peak_, bytes_);
  return true;
}
size_t WebSocketOutput::Bytes() const {
  const std::lock_guard lock(mutex_);
  return bytes_;
}
size_t WebSocketOutput::PeakBytes() const {
  const std::lock_guard lock(mutex_);
  return peak_;
}
bool WebSocketOutput::Closed() const {
  const std::lock_guard lock(mutex_);
  return closed_;
}
std::string_view WebSocketOutput::Front() const {
  const std::lock_guard lock(mutex_);
  return frames_.empty() ? std::string_view() : frames_.front();
}
void WebSocketOutput::Acknowledge() {
  {
    const std::lock_guard lock(mutex_);
    if (!frames_.empty()) {
      bytes_ -= frames_.front().capacity();
      frames_.pop_front();
    }
  }
  changed_.notify_all();
}
void WebSocketOutput::Close(bool retain_front) {
  {
    const std::lock_guard lock(mutex_);
    closed_ = true;
    while (frames_.size() > size_t(retain_front)) frames_.pop_back();
    bytes_ = frames_.empty() ? 0 : frames_.front().capacity();
  }
  changed_.notify_all();
}

WebSocketSessionResult RunWebSocketSession(int fd, std::string_view initial,
                                           WebSocketOutput& output,
                                           const WebSocketSessionCallbacks& cb,
                                           WebSocketSessionLimits limits) {
  WebSocketSessionResult result;
  bool cancelled = false;
  const auto cancel = [&]() noexcept {
    if (cancelled) return;
    cancelled = true;
    try {
      if (cb.cancel) cb.cancel();
    } catch (...) {
    }
  };
  struct Cleanup {
    const decltype(cancel)& invoke;
    WebSocketOutput& output;
    ~Cleanup() {
      invoke();
      output.Close();
    }
  } cleanup{cancel, output};
  const auto finish = [&] {
    cancel();
    output.Close();
    result.peak_output_bytes = output.PeakBytes();
    return result;
  };
  if (fd < 0 || initial.size() > 65536 || !output.Valid() || !cb.message ||
      limits.message_bytes == 0 ||
      limits.message_bytes > kWebSocketMessageLimit ||
      limits.idle.count() < 1 || limits.write.count() < 1 ||
      limits.session.count() < 1 || limits.idle > std::chrono::hours(1) ||
      limits.write > std::chrono::seconds(30) ||
      limits.session > std::chrono::hours(1))
    return finish();
  WebSocketDecoder decoder(true, limits.message_bytes);
  std::array<uint8_t, 16384> input{};
  auto pending = std::span(reinterpret_cast<const uint8_t*>(initial.data()),
                           initial.size());
  // The initial HTTP tail was received before this loop's byte counter.
  result.received_bytes = pending.size();
  std::string control_frame, next_control;
  std::string_view writing;
  size_t sent = 0;
  bool application_frame = false, closing = false;
  auto now = Clock::now(), active = now;
  const auto deadline = now + limits.session;
  auto write_deadline = Clock::time_point::max();
  auto close_deadline = Clock::time_point::max();
  const auto close = [&](WebSocketSessionEnd end, uint16_t code,
                         std::string_view peer_payload = {}) {
    if (closing) return;
    closing = true;
    result.end = end;
    result.close_code = code;
    cancel();
    const bool retain = application_frame && sent != 0;
    if (application_frame && !retain) {
      writing = {};
      application_frame = false;
    }
    output.Close(retain);
    std::string payload = peer_payload.empty()
                              ? std::string({char(code >> 8), char(code & 255)})
                              : std::string(peer_payload);
    if (end == WebSocketSessionEnd::kPeerClose && code == 0) payload.clear();
    EncodeWebSocketFrame(WebSocketOpcode::kClose, payload, std::nullopt,
                         &next_control);
    close_deadline =
        Clock::now() + std::min(limits.write, std::chrono::milliseconds(1000));
  };
  try {
    for (;;) {
      now = Clock::now();
      if (!closing) {
        if (cb.stopped && cb.stopped())
          close(WebSocketSessionEnd::kShutdown, 1001);
        else if (cb.authorized && !cb.authorized())
          close(WebSocketSessionEnd::kRevoked, 1008);
        else if (now >= deadline)
          close(WebSocketSessionEnd::kSessionDeadline, 1001);
        else if (now - active >= limits.idle)
          close(WebSocketSessionEnd::kIdleDeadline, 1001);
      }
      if (closing && now >= close_deadline) return finish();
      if (!writing.empty() && now >= write_deadline) {
        result.end = WebSocketSessionEnd::kWriteDeadline;
        return finish();
      }
      // Bounded batches keep input floods from starving output or cancellation.
      for (int events = 0; !closing && !pending.empty() && events < 8;
           ++events) {
        size_t consumed = 0;
        const auto state = decoder.Consume(pending, &consumed);
        pending = pending.subspan(consumed);
        if (state == WebSocketRead::kFailed) {
          close(WebSocketSessionEnd::kProtocol, decoder.FailureCode());
          break;
        }
        if (state != WebSocketRead::kEvent) break;
        const auto event = decoder.Event();
        if (event.opcode == WebSocketOpcode::kClose) {
          const auto code = event.payload.size() >= 2
                                ? (uint16_t(uint8_t(event.payload[0])) << 8) |
                                      uint8_t(event.payload[1])
                                : 0;
          close(WebSocketSessionEnd::kPeerClose, code, event.payload);
        } else if (event.opcode == WebSocketOpcode::kPing) {
          // RFC6455 permits responding only to the latest pending ping. A
          // partially written pong stays untouched in control_frame.
          EncodeWebSocketFrame(WebSocketOpcode::kPong, event.payload,
                               std::nullopt, &next_control);
        } else if (event.opcode != WebSocketOpcode::kPong) {
          ++result.messages;
          if (!cb.message(event.opcode, event.payload))
            close(WebSocketSessionEnd::kApplication, 1011);
        }
        decoder.Acknowledge();
      }
      if (writing.empty()) {
        if (!next_control.empty()) {
          control_frame = std::move(next_control);
          next_control.clear();
          writing = control_frame;
          application_frame = false;
        } else if (!closing) {
          writing = output.Front();
          application_frame = !writing.empty();
        }
        if (!writing.empty()) {
          sent = 0;
          write_deadline = Clock::now() + limits.write;
        }
      }
      if (!writing.empty()) {
        const auto count =
            ::send(fd, writing.data() + sent, writing.size() - sent,
                   MSG_DONTWAIT | MSG_NOSIGNAL);
        if (count > 0) {
          sent += count;
          result.sent_bytes += count;
          active = Clock::now();
          if (sent == writing.size()) {
            const bool was_close =
                !application_frame && (uint8_t(writing[0]) & 15) == 8;
            writing = {};
            if (application_frame)
              output.Acknowledge();
            else
              control_frame.clear();
            application_frame = false;
            if (was_close) return finish();
          }
        } else if (count == 0 || (errno != EAGAIN && errno != EWOULDBLOCK &&
                                  errno != EINTR)) {
          result.end = WebSocketSessionEnd::kIo;
          return finish();
        }
      }
      if (!closing && pending.empty()) {
        const auto count = ::recv(fd, input.data(), input.size(), MSG_DONTWAIT);
        if (count > 0) {
          pending = std::span(input).first(count);
          result.received_bytes += count;
          active = Clock::now();
          continue;
        }
        if (count == 0) {
          decoder.EndOfInput();
          result.end = WebSocketSessionEnd::kPeerEof;
          result.close_code = 1006;
          return finish();
        }
        if (errno != EAGAIN && errno != EWOULDBLOCK && errno != EINTR) {
          result.end = WebSocketSessionEnd::kIo;
          return finish();
        }
      }
      if (!pending.empty() && !closing) continue;
      pollfd poller{
          fd, short((closing ? 0 : POLLIN) | (writing.empty() ? 0 : POLLOUT)),
          0};
      const auto ready = ::poll(&poller, 1, 5);
      if (ready < 0 && errno != EINTR) {
        result.end = WebSocketSessionEnd::kIo;
        return finish();
      }
    }
  } catch (...) {
    // A local exception closes this upgraded connection. HTTP catch paths are
    // explicitly sealed and cannot append an invalid second response.
    result.end = WebSocketSessionEnd::kApplication;
    result.close_code = 1011;
    return finish();
  }
}
}  // namespace q4t::server
