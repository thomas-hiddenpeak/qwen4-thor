#include "q4t/server/websocket.h"

#include <algorithm>
#include <cstring>

#include "q4t/server/native_crypto.h"

namespace q4t::server {
namespace {
bool EqualAscii(std::string_view a, std::string_view b) {
  if (a.size() != b.size()) return false;
  for (size_t i = 0; i < a.size(); ++i) {
    auto c = a[i];
    if (c >= 'A' && c <= 'Z') c += 'a' - 'A';
    if (c != b[i]) return false;
  }
  return true;
}
bool Control(WebSocketOpcode opcode) {
  return static_cast<uint8_t>(opcode) >= 8;
}
bool ValidOpcode(uint8_t value) {
  return value <= 2 || value == 8 || value == 9 || value == 10;
}
bool CloseCode(uint16_t code) {
  return (code >= 1000 && code <= 1014 && code != 1004 && code != 1005 &&
          code != 1006) ||
         (code >= 3000 && code < 5000);
}
bool Utf8Byte(uint8_t byte, uint8_t* remaining, uint8_t* minimum,
              uint8_t* maximum) {
  if (*remaining) {
    if (byte < *minimum || byte > *maximum) return false;
    --*remaining;
    *minimum = 0x80;
    *maximum = 0xbf;
    return true;
  }
  if (byte <= 0x7f) return true;
  if (byte < 0xc2 || byte > 0xf4) return false;
  *remaining = byte < 0xe0 ? 1 : byte < 0xf0 ? 2 : 3;
  *minimum = byte == 0xe0 ? 0xa0 : byte == 0xf0 ? 0x90 : 0x80;
  *maximum = byte == 0xed ? 0x9f : byte == 0xf4 ? 0x8f : 0xbf;
  return true;
}
bool ValidUtf8(std::string_view bytes) {
  uint8_t remaining = 0, minimum = 0x80, maximum = 0xbf;
  for (uint8_t byte : bytes)
    if (!Utf8Byte(byte, &remaining, &minimum, &maximum)) return false;
  return remaining == 0;
}
uint16_t CloseError(std::string_view bytes) {
  if (bytes.empty()) return 0;
  if (bytes.size() == 1) return 1002;
  const auto code =
      (static_cast<uint8_t>(bytes[0]) << 8) | static_cast<uint8_t>(bytes[1]);
  if (!CloseCode(code)) return 1002;
  return ValidUtf8(bytes.substr(2)) ? 0 : 1007;
}
}  // namespace

WebSocketUpgradeResult CheckWebSocketUpgrade(const WebSocketUpgrade& request,
                                             std::string* accept) {
  if (!accept || request.method != "GET" || !request.http11 ||
      request.chunked || request.body_bytes ||
      !EqualAscii(request.upgrade, "websocket") ||
      request.connection.size() > 1024)
    return WebSocketUpgradeResult::kBadRequest;
  bool upgrade = false;
  auto connection = request.connection;
  while (!connection.empty()) {
    const auto comma = connection.find(',');
    auto word = connection.substr(0, comma);
    while (!word.empty() && (word.front() == ' ' || word.front() == '\t'))
      word.remove_prefix(1);
    while (!word.empty() && (word.back() == ' ' || word.back() == '\t'))
      word.remove_suffix(1);
    if (word.empty() || EqualAscii(word, "close"))
      return WebSocketUpgradeResult::kBadRequest;
    for (unsigned char c : word)
      if (c <= 32 || c >= 127 ||
          std::string_view("()<>@,;:\\\"/[]?={}").find(c) !=
              std::string_view::npos)
        return WebSocketUpgradeResult::kBadRequest;
    upgrade |= EqualAscii(word, "upgrade");
    if (comma == connection.npos) break;
    connection.remove_prefix(comma + 1);
    if (connection.empty()) return WebSocketUpgradeResult::kBadRequest;
  }
  if (!upgrade) return WebSocketUpgradeResult::kBadRequest;
  if (request.version != "13") return WebSocketUpgradeResult::kWrongVersion;
  switch (CryptoWebSocketAccept(request.key, accept)) {
    case CryptoResult::kOk:
      return WebSocketUpgradeResult::kOk;
    case CryptoResult::kInvalid:
      return WebSocketUpgradeResult::kBadRequest;
    case CryptoResult::kUnavailable:
      return WebSocketUpgradeResult::kCryptoUnavailable;
    default:
      return WebSocketUpgradeResult::kCryptoFailure;
  }
}

bool WebSocketDecoder::Utf8::Add(uint8_t byte) {
  return Utf8Byte(byte, &remaining, &minimum, &maximum);
}
WebSocketDecoder::WebSocketDecoder(bool incoming_masked, size_t maximum_bytes)
    : incoming_masked_(incoming_masked), maximum_bytes_(maximum_bytes) {
  if (!maximum_bytes || maximum_bytes > kWebSocketMaximumLimit) Fail(1009);
}
WebSocketRead WebSocketDecoder::Fail(uint16_t code) {
  failure_code_ = code;
  ready_ = false;
  message_bytes_ = control_bytes_ = 0;
  return WebSocketRead::kFailed;
}
bool WebSocketDecoder::StartFrame() {
  const auto opcode = header_[0] & 0xf;
  final_ = (header_[0] & 0x80) != 0;
  const bool masked = (header_[1] & 0x80) != 0;
  if ((header_[0] & 0x70) || !ValidOpcode(opcode) ||
      masked != incoming_masked_) {
    Fail(1002);
    return false;
  }
  frame_opcode_ = static_cast<WebSocketOpcode>(opcode);
  const uint8_t length_code = header_[1] & 0x7f;
  if (Control(frame_opcode_) && (!final_ || length_code > 125)) {
    Fail(1002);
    return false;
  }
  const size_t extended = length_code == 126 ? 2 : length_code == 127 ? 8 : 0;
  const size_t needed = 2 + extended + (masked ? 4 : 0);
  if (header_bytes_ < needed) {
    header_needed_ = needed;
    return true;
  }
  uint64_t length = extended ? 0 : length_code;
  for (size_t i = 0; i < extended; ++i) length = (length << 8) | header_[2 + i];
  if ((extended == 2 && length < 126) ||
      (extended == 8 && (length <= 65535 || (header_[2] & 0x80)))) {
    Fail(1002);
    return false;
  }
  if (!Control(frame_opcode_)) {
    if ((frame_opcode_ == WebSocketOpcode::kContinuation) != fragmented_) {
      Fail(1002);
      return false;
    }
    if (length > maximum_bytes_ - message_bytes_) {
      Fail(1009);
      return false;
    }
    if (!fragmented_) {
      message_opcode_ = frame_opcode_;
      utf8_ = {};
    }
    if (length && !message_)
      message_ = std::make_unique<char[]>(maximum_bytes_);
  }
  frame_bytes_ = length;
  frame_read_ = 0;
  if (masked)
    std::copy_n(header_.begin() + 2 + extended, 4, mask_.begin());
  else
    mask_.fill(0);
  control_bytes_ = 0;
  frame_started_ = true;
  return true;
}
bool WebSocketDecoder::FinishFrame() {
  if (Control(frame_opcode_)) {
    if (frame_opcode_ == WebSocketOpcode::kClose) {
      if (const auto error = CloseError({control_.data(), control_bytes_})) {
        Fail(error);
        return false;
      }
      close_received_ = true;
    }
    event_opcode_ = frame_opcode_;
    ready_ = true;
  } else {
    fragmented_ = !final_;
    if (final_) {
      if (message_opcode_ == WebSocketOpcode::kText && utf8_.remaining) {
        Fail(1007);
        return false;
      }
      event_opcode_ = message_opcode_;
      ready_ = true;
    }
  }
  header_bytes_ = 0;
  header_needed_ = 2;
  frame_started_ = false;
  return true;
}
WebSocketRead WebSocketDecoder::Consume(std::span<const uint8_t> input,
                                        size_t* consumed) {
  if (!consumed) return Fail(1002);
  *consumed = 0;
  if (failure_code_) return WebSocketRead::kFailed;
  if (ready_) return WebSocketRead::kBlocked;
  if (close_received_) return WebSocketRead::kClosed;
  while (*consumed < input.size() || frame_started_) {
    if (!frame_started_) {
      while (header_bytes_ < header_needed_ && *consumed < input.size())
        header_[header_bytes_++] = input[(*consumed)++];
      if (header_bytes_ < header_needed_) return WebSocketRead::kNeedInput;
      if (!StartFrame()) return WebSocketRead::kFailed;
      if (!frame_started_) continue;
    }
    const auto count =
        std::min(frame_bytes_ - frame_read_, input.size() - *consumed);
    for (size_t i = 0; i < count; ++i) {
      const uint8_t byte = input[(*consumed)++] ^ mask_[frame_read_++ % 4];
      if (Control(frame_opcode_)) {
        control_[control_bytes_++] = static_cast<char>(byte);
      } else {
        if (message_opcode_ == WebSocketOpcode::kText && !utf8_.Add(byte))
          return Fail(1007);
        message_[message_bytes_++] = static_cast<char>(byte);
      }
    }
    if (frame_read_ < frame_bytes_) return WebSocketRead::kNeedInput;
    if (!FinishFrame()) return WebSocketRead::kFailed;
    if (ready_) return WebSocketRead::kEvent;
  }
  return WebSocketRead::kNeedInput;
}
WebSocketEvent WebSocketDecoder::Event() const {
  if (!ready_) return {};
  return {event_opcode_, Control(event_opcode_)
                             ? std::string_view(control_.data(), control_bytes_)
                             : std::string_view(message_ ? message_.get() : "",
                                                message_bytes_)};
}
Status WebSocketDecoder::Acknowledge() {
  if (!ready_ || failure_code_)
    return Status::Fail("no WebSocket event pending");
  if (Control(event_opcode_))
    control_bytes_ = 0;
  else
    message_bytes_ = 0;
  ready_ = false;
  return {};
}
WebSocketRead WebSocketDecoder::EndOfInput() {
  if (failure_code_) return WebSocketRead::kFailed;
  return close_received_ ? WebSocketRead::kClosed : Fail(1006);
}

Status EncodeWebSocketFrame(WebSocketOpcode opcode, std::string_view payload,
                            std::optional<std::array<uint8_t, 4>> mask,
                            std::string* output, size_t maximum_bytes) {
  if (!output || !maximum_bytes || maximum_bytes > kWebSocketMaximumLimit ||
      (!Control(opcode) && payload.size() > maximum_bytes) ||
      !ValidOpcode(static_cast<uint8_t>(opcode)) ||
      opcode == WebSocketOpcode::kContinuation ||
      (Control(opcode) && payload.size() > 125) ||
      (opcode == WebSocketOpcode::kText && !ValidUtf8(payload)) ||
      (opcode == WebSocketOpcode::kClose && CloseError(payload)))
    return Status::Fail("invalid WebSocket output frame");
  std::string result;
  result.reserve(payload.size() + 14);
  result += static_cast<char>(0x80 | static_cast<uint8_t>(opcode));
  const auto masked = mask ? 0x80 : 0;
  const auto size = payload.size();
  if (size <= 125)
    result += static_cast<char>(masked | size);
  else {
    result += static_cast<char>(masked | (size <= 65535 ? 126 : 127));
    for (int shift = size <= 65535 ? 8 : 56; shift >= 0; shift -= 8)
      result +=
          static_cast<char>((static_cast<uint64_t>(size) >> shift) & 0xff);
  }
  if (mask)
    for (auto byte : *mask) result += static_cast<char>(byte);
  for (size_t i = 0; i < size; ++i)
    result += static_cast<char>(static_cast<uint8_t>(payload[i]) ^
                                (mask ? (*mask)[i % 4] : 0));
  *output = std::move(result);
  return {};
}
}  // namespace q4t::server
