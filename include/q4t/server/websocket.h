// Bounded RFC6455 framing shared by native ingress and model connections.
#pragma once

#include <array>
#include <cstddef>
#include <cstdint>
#include <memory>
#include <optional>
#include <span>
#include <string>
#include <string_view>

#include "q4t/status.h"

namespace q4t::server {
inline constexpr size_t kWebSocketMessageLimit = 16ULL << 20;
inline constexpr size_t kWebSocketMaximumLimit = 64ULL << 20;
enum class WebSocketOpcode : uint8_t {
  kContinuation = 0,
  kText = 1,
  kBinary = 2,
  kClose = 8,
  kPing = 9,
  kPong = 10,
};
enum class WebSocketRead { kNeedInput, kEvent, kBlocked, kClosed, kFailed };
enum class WebSocketUpgradeResult {
  kOk,
  kBadRequest,
  kWrongVersion,
  kCryptoUnavailable,
  kCryptoFailure
};
struct WebSocketUpgrade {
  std::string_view method, connection, upgrade, version, key;
  bool http11 = false;
  bool chunked = false;
  size_t body_bytes = 0;
};
// Header duplicates must already have been rejected by the shared HTTP reader.
// This does not authorize a connection: route/client/origin/quota checks
// precede it. No extensions are negotiated. Only success changes accept.
WebSocketUpgradeResult CheckWebSocketUpgrade(const WebSocketUpgrade& request,
                                             std::string* accept);

struct WebSocketEvent {
  WebSocketOpcode opcode = WebSocketOpcode::kContinuation;
  // Owned by the decoder; remains stable until Acknowledge or EndOfInput.
  std::string_view payload;
};

class WebSocketDecoder {
 public:
  // Incoming client frames require masking; server frames forbid masking.
  // A caller reserves maximum_bytes before using this decoder. Storage is
  // allocated once on the first nonempty data frame, and never grows beyond
  // that exact byte array; header/control storage is fixed inside the object.
  explicit WebSocketDecoder(bool incoming_masked = true,
                            size_t maximum_bytes = kWebSocketMessageLimit);
  WebSocketRead Consume(std::span<const uint8_t> input, size_t* consumed);
  WebSocketEvent Event() const;
  // Pending output blocks Consume without consuming or overwriting anything.
  Status Acknowledge();
  WebSocketRead EndOfInput();
  uint16_t FailureCode() const { return failure_code_; }
  size_t StorageBytes() const { return message_ ? maximum_bytes_ : 0; }

 private:
  struct Utf8 {
    bool Add(uint8_t byte);
    uint8_t remaining = 0, minimum = 0x80, maximum = 0xbf;
  };
  WebSocketRead Fail(uint16_t code);
  bool StartFrame();
  bool FinishFrame();
  const bool incoming_masked_;
  const size_t maximum_bytes_;
  std::array<uint8_t, 14> header_{};
  size_t header_bytes_ = 0, header_needed_ = 2;
  std::array<char, 125> control_{};
  size_t control_bytes_ = 0;
  std::unique_ptr<char[]> message_;
  size_t message_bytes_ = 0;
  Utf8 utf8_;
  std::array<uint8_t, 4> mask_{};
  size_t frame_bytes_ = 0, frame_read_ = 0;
  WebSocketOpcode frame_opcode_ = WebSocketOpcode::kContinuation;
  WebSocketOpcode message_opcode_ = WebSocketOpcode::kContinuation;
  WebSocketOpcode event_opcode_ = WebSocketOpcode::kContinuation;
  uint16_t failure_code_ = 0;
  bool frame_started_ = false, final_ = false, fragmented_ = false;
  bool ready_ = false, close_received_ = false;
};

// One complete FIN frame, no fragmentation or extensions. Client-side callers
// supply a fresh unpredictable mask for every frame; nullopt is server output.
// Validate text/close, size and opcode before replacing the output buffer.
Status EncodeWebSocketFrame(WebSocketOpcode opcode, std::string_view payload,
                            std::optional<std::array<uint8_t, 4>> mask,
                            std::string* output,
                            size_t maximum_bytes = kWebSocketMessageLimit);
}  // namespace q4t::server
