#include "q4t/server/websocket.h"

#include <algorithm>
#include <vector>

#include "q4t/server/native_crypto.h"
#include "q4t/test.h"

namespace {
using namespace q4t::server;
std::span<const uint8_t> Bytes(std::string_view value) {
  return {reinterpret_cast<const uint8_t*>(value.data()), value.size()};
}
std::string Raw(uint8_t opcode, std::string_view payload, bool final = true,
                bool masked = true) {
  const std::array<uint8_t, 4> key{0x37, 0xfa, 0x21, 0x3d};
  std::string frame(1, static_cast<char>((final ? 0x80 : 0) | opcode));
  const auto size = payload.size();
  frame += static_cast<char>((masked ? 0x80 : 0) | (size < 126     ? size
                                                    : size < 65536 ? 126
                                                                   : 127));
  if (size >= 126)
    for (int n = size < 65536 ? 1 : 7; n >= 0; --n)
      frame +=
          static_cast<char>((static_cast<uint64_t>(size) >> (8 * n)) & 255);
  if (masked)
    for (auto byte : key) frame += static_cast<char>(byte);
  for (size_t i = 0; i < size; ++i)
    frame += static_cast<char>(static_cast<uint8_t>(payload[i]) ^
                               (masked ? key[i % 4] : 0));
  return frame;
}
std::string Close(uint16_t code, std::string_view reason = {}) {
  return std::string{static_cast<char>(code >> 8),
                     static_cast<char>(code & 255)} +
         std::string(reason);
}
bool Rejected(const std::string& wire, uint16_t code, size_t maximum = 1024,
              bool masked = true) {
  WebSocketDecoder parser(masked, maximum);
  size_t consumed = 0;
  if (parser.Consume(Bytes(wire), &consumed) != WebSocketRead::kFailed ||
      parser.FailureCode() != code || parser.Acknowledge() ||
      !parser.Event().payload.empty())
    return false;
  consumed = 55;
  return parser.Consume(Bytes(Raw(1, "later")), &consumed) ==
             WebSocketRead::kFailed &&
         consumed == 0;
}
WebSocketUpgrade Upgrade() {
  return {"GET",
          "keep-alive, Upgrade",
          "WebSocket",
          "13",
          "dGhlIHNhbXBsZSBub25jZQ==",
          true,
          false,
          0};
}
}  // namespace

Q4T_TEST(websocket_handshake_rfc_example_and_canonical_keys) {
  std::string accept;
  Q4T_CHECK(CheckWebSocketUpgrade(Upgrade(), &accept) ==
            WebSocketUpgradeResult::kOk);
  Q4T_CHECK(accept == "s3pPLMBiTxaQ9kYGzzhZRbK+xOo=");
  for (const auto key : {"", "dGhlIHNhbXBsZSBub25jZQ=",  // short
                         "dGhlIHNhbXBsZSBub25jZR==",     // nonzero pad bits
                         "dGhlIHNhbXBsZSBub25jZ!==",     // illegal alphabet
                         " dGhlIHNhbXBsZSBub25jZQ=="}) {
    auto request = Upgrade();
    request.key = key;
    accept = "unchanged";
    Q4T_CHECK(CheckWebSocketUpgrade(request, &accept) ==
              WebSocketUpgradeResult::kBadRequest);
    Q4T_CHECK(accept == "unchanged");
  }
  return true;
}

Q4T_TEST(websocket_handshake_rejects_transport_ambiguity) {
  for (int field = 0; field < 11; ++field) {
    auto request = Upgrade();
    if (field == 0) request.method = "POST";
    if (field == 1) request.http11 = false;
    if (field == 2) request.chunked = true;
    if (field == 3) request.body_bytes = 1;
    if (field == 4) request.connection = "keep-alive";
    if (field == 5) request.connection = "upgrade, close";
    if (field == 6) request.connection = "upgrade,";
    if (field == 7) request.connection = "up grade";
    if (field == 8) request.upgrade = "websocket, other";
    if (field == 9) request.version = "12";
    if (field == 10) request.connection = "upgrade, \r\ninjected";
    std::string result = "unchanged";
    Q4T_CHECK(CheckWebSocketUpgrade(request, &result) ==
              (field == 9 ? WebSocketUpgradeResult::kWrongVersion
                          : WebSocketUpgradeResult::kBadRequest));
    Q4T_CHECK(result == "unchanged");
  }
  return true;
}

#ifdef Q4T_GRAMMAR_PROCESS_TESTS
Q4T_TEST(websocket_crypto_failure_is_local_and_recovers) {
  struct Reset {
    ~Reset() { SetCryptoUnavailableForTesting(false); }
  } reset;
  SetCryptoUnavailableForTesting(true);
  std::string accept = "unchanged";
  Q4T_CHECK(CheckWebSocketUpgrade(Upgrade(), &accept) ==
            WebSocketUpgradeResult::kCryptoUnavailable);
  Q4T_CHECK(accept == "unchanged");
  // Framing has no crypto dependency; an unrelated healthy connection works.
  WebSocketDecoder parser(true, 16);
  size_t consumed = 0;
  Q4T_CHECK(parser.Consume(Bytes(Raw(1, "healthy")), &consumed) ==
            WebSocketRead::kEvent);
  Q4T_CHECK(parser.Event().payload == "healthy");
  SetCryptoUnavailableForTesting(false);
  Q4T_CHECK(CheckWebSocketUpgrade(Upgrade(), &accept) ==
            WebSocketUpgradeResult::kOk);
  return true;
}
#endif

Q4T_TEST(websocket_rfc_mask_vector_every_input_split) {
  const std::string wire("\x81\x85\x37\xfa\x21\x3d\x7f\x9f\x4d\x51\x58", 11);
  for (size_t cut = 0; cut <= wire.size(); ++cut) {
    WebSocketDecoder parser(true, 8);
    size_t consumed = 0;
    auto state = parser.Consume(Bytes(wire).first(cut), &consumed);
    Q4T_CHECK(consumed == cut);
    if (cut < wire.size()) {
      Q4T_CHECK(state == WebSocketRead::kNeedInput);
      state = parser.Consume(Bytes(wire).subspan(cut), &consumed);
      Q4T_CHECK(consumed == wire.size() - cut);
    }
    Q4T_CHECK(state == WebSocketRead::kEvent);
    Q4T_CHECK(parser.Event().opcode == WebSocketOpcode::kText);
    Q4T_CHECK(parser.Event().payload == "Hello" && parser.StorageBytes() == 8);
  }
  return true;
}

Q4T_TEST(websocket_output_ack_blocks_coalesced_input_without_overwrite) {
  const auto first = Raw(1, "first"), second = Raw(2, std::string("a\0b", 3));
  WebSocketDecoder parser(true, 16);
  size_t used = 0;
  Q4T_CHECK(parser.Consume(Bytes(first + second), &used) ==
            WebSocketRead::kEvent);
  Q4T_CHECK(used == first.size());
  const auto view = parser.Event().payload;
  Q4T_CHECK(parser.Consume(Bytes(second), &used) == WebSocketRead::kBlocked);
  Q4T_CHECK(used == 0 && view == "first" &&
            parser.Event().payload.data() == view.data());
  Q4T_CHECK(parser.Acknowledge());
  Q4T_CHECK(!parser.Acknowledge());
  Q4T_CHECK(parser.Consume(Bytes(second), &used) == WebSocketRead::kEvent);
  Q4T_CHECK(parser.Event().opcode == WebSocketOpcode::kBinary);
  Q4T_CHECK(parser.Event().payload == std::string("a\0b", 3));
  return true;
}

Q4T_TEST(websocket_fragmented_utf8_and_interleaved_control_keep_state) {
  const std::string text = "你🙂好";
  const auto prefix = Raw(1, std::string_view(text).substr(0, 1), false);
  const auto middle = Raw(0, std::string_view(text).substr(1, 4), false);
  const auto suffix = Raw(0, std::string_view(text).substr(5));
  const auto ping = Raw(9, std::string("\0\xff", 2));
  const auto wire = prefix + ping + middle + suffix;
  WebSocketDecoder parser(true, 32);
  size_t used = 0;
  Q4T_CHECK(parser.Consume(Bytes(wire), &used) == WebSocketRead::kEvent);
  Q4T_CHECK(used == prefix.size() + ping.size());
  Q4T_CHECK(parser.Event().opcode == WebSocketOpcode::kPing);
  Q4T_CHECK(parser.Event().payload == std::string("\0\xff", 2));
  Q4T_CHECK(parser.Acknowledge());
  for (auto byte : Bytes(wire).subspan(used)) {
    size_t n = 0;
    const auto result = parser.Consume({&byte, 1}, &n);
    Q4T_CHECK(n == 1 && result != WebSocketRead::kFailed);
  }
  Q4T_CHECK(parser.Event().opcode == WebSocketOpcode::kText);
  Q4T_CHECK(parser.Event().payload == text);
  return true;
}

Q4T_TEST(websocket_rejects_utf8_overlong_surrogate_and_truncated_final) {
  for (const auto bytes :
       {"\x80", "\xc0\xaf", "\xe0\x80\x80", "\xed\xa0\x80", "\xf4\x90\x80\x80",
        "\xf5\x80\x80\x80", "\xc2", "\xe4\xbd"})
    Q4T_CHECK(Rejected(Raw(1, bytes), 1007));
  Q4T_CHECK(Rejected(Raw(1, "\xe4", false) + Raw(0, "a"), 1007));
  return true;
}

Q4T_TEST(websocket_rejects_reserved_opcode_mask_and_bad_fragment_sequence) {
  for (const auto opcode : {3, 4, 5, 6, 7, 11, 12, 13, 14, 15})
    Q4T_CHECK(Rejected(Raw(opcode, ""), 1002));
  for (const auto reserved : {0x10, 0x20, 0x40}) {
    auto raw = Raw(1, "a");
    raw[0] |= reserved;
    Q4T_CHECK(Rejected(raw, 1002));
  }
  Q4T_CHECK(Rejected(Raw(1, "a", true, false), 1002));
  Q4T_CHECK(Rejected(Raw(1, "a"), 1002, 1024, false));
  Q4T_CHECK(Rejected(Raw(0, "a"), 1002));
  Q4T_CHECK(Rejected(Raw(1, "a", false) + Raw(2, "b"), 1002));
  return true;
}

Q4T_TEST(websocket_control_and_close_validation) {
  Q4T_CHECK(Rejected(Raw(9, "", false), 1002));
  Q4T_CHECK(Rejected(Raw(10, std::string(126, 'x')), 1002));
  Q4T_CHECK(Rejected(Raw(8, "x"), 1002));
  for (uint16_t code : {999, 1004, 1005, 1006, 1015, 2000, 5000})
    Q4T_CHECK(Rejected(Raw(8, Close(code)), 1002));
  Q4T_CHECK(Rejected(Raw(8, Close(1000, "\xc0\xaf")), 1007));
  for (const auto& value : {std::string{}, Close(1000, "完成"), Close(4001)}) {
    WebSocketDecoder parser;
    size_t used = 0;
    Q4T_CHECK(parser.Consume(Bytes(Raw(8, value) + Raw(1, "late")), &used) ==
              WebSocketRead::kEvent);
    Q4T_CHECK(parser.Event().opcode == WebSocketOpcode::kClose);
    Q4T_CHECK(parser.Event().payload == value);
    Q4T_CHECK(parser.Acknowledge());
    Q4T_CHECK(parser.Consume(Bytes(Raw(1, "late")), &used) ==
              WebSocketRead::kClosed);
    Q4T_CHECK(used == 0 && parser.EndOfInput() == WebSocketRead::kClosed);
  }
  return true;
}

Q4T_TEST(websocket_canonical_extended_lengths_and_exact_storage_budget) {
  for (const size_t size : {0, 125, 126, 65535, 65536}) {
    const std::string payload(size, 'x');
    const auto wire = Raw(2, payload);
    WebSocketDecoder parser(true, std::max(size, size_t{1}));
    size_t used = 0;
    Q4T_CHECK(parser.Consume(Bytes(wire), &used) == WebSocketRead::kEvent);
    Q4T_CHECK(used == wire.size() && parser.Event().payload == payload);
    Q4T_CHECK(parser.StorageBytes() == size);
  }
  return true;
}

Q4T_TEST(websocket_invalid_and_oversized_lengths_reject_before_allocation) {
  for (const std::string& wire :
       {std::string("\x82\xfe\0\x7d", 4),
        std::string("\x82\xff\0\0\0\0\0\0\xff\xff", 10),
        std::string("\x82\xff\x80\0\0\0\0\0\0\0", 10)}) {
    // Add the mask: length canonicality is checked after a complete header.
    Q4T_CHECK(Rejected(wire + std::string(4, '\0'), 1002));
  }
  WebSocketDecoder parser(true, 128);
  const std::string large("\x82\xff\0\0\0\x01\0\0\0\0\0\0\0\0", 14);
  size_t used = 0;
  Q4T_CHECK(parser.Consume(Bytes(large), &used) == WebSocketRead::kFailed);
  Q4T_CHECK(parser.FailureCode() == 1009 && parser.StorageBytes() == 0);
  Q4T_CHECK(Rejected(Raw(1, "abc", false) + Raw(0, "de"), 1009, 4));
  return true;
}

Q4T_TEST(websocket_eof_and_invalid_configuration_cannot_fake_completion) {
  const auto wire = Raw(1, "hello");
  for (size_t cut = 0; cut < wire.size(); ++cut) {
    WebSocketDecoder parser;
    size_t used = 0;
    Q4T_CHECK(parser.Consume(Bytes(wire).first(cut), &used) ==
              WebSocketRead::kNeedInput);
    Q4T_CHECK(parser.EndOfInput() == WebSocketRead::kFailed);
    Q4T_CHECK(parser.FailureCode() == 1006);
  }
  for (const auto limit : {size_t{0}, kWebSocketMaximumLimit + 1}) {
    WebSocketDecoder parser(true, limit);
    size_t used = 99;
    Q4T_CHECK(parser.Consume(Bytes(wire), &used) == WebSocketRead::kFailed);
    Q4T_CHECK(used == 0 && parser.StorageBytes() == 0);
  }
  return true;
}

Q4T_TEST(websocket_encoder_rfc_mask_and_unmasked_empty_control) {
  std::string wire;
  Q4T_CHECK(EncodeWebSocketFrame(WebSocketOpcode::kText, "Hello",
                                 std::array<uint8_t, 4>{0x37, 0xfa, 0x21, 0x3d},
                                 &wire));
  Q4T_CHECK(wire ==
            std::string("\x81\x85\x37\xfa\x21\x3d\x7f\x9f\x4d\x51\x58", 11));
  Q4T_CHECK(
      EncodeWebSocketFrame(WebSocketOpcode::kPing, "", std::nullopt, &wire));
  Q4T_CHECK(wire == std::string("\x89\0", 2));
  for (const size_t size : {126, 65536}) {
    const std::string payload(size, 'x');
    Q4T_CHECK(EncodeWebSocketFrame(WebSocketOpcode::kBinary, payload,
                                   std::nullopt, &wire));
    Q4T_CHECK(wire == Raw(2, payload, true, false));
  }
  return true;
}

Q4T_TEST(websocket_encoder_failures_preserve_previous_output) {
  std::string result = "unchanged";
  for (const auto opcode :
       {WebSocketOpcode::kContinuation, static_cast<WebSocketOpcode>(15)})
    Q4T_CHECK(!EncodeWebSocketFrame(opcode, "x", std::nullopt, &result));
  Q4T_CHECK(!EncodeWebSocketFrame(WebSocketOpcode::kText, "\x80", std::nullopt,
                                  &result));
  Q4T_CHECK(!EncodeWebSocketFrame(WebSocketOpcode::kClose, Close(1006),
                                  std::nullopt, &result));
  Q4T_CHECK(!EncodeWebSocketFrame(WebSocketOpcode::kPing, std::string(126, 'a'),
                                  std::nullopt, &result));
  Q4T_CHECK(!EncodeWebSocketFrame(WebSocketOpcode::kText, "12345", std::nullopt,
                                  &result, 4));
  Q4T_CHECK(result == "unchanged");
  return true;
}
