#include "q4t/server/http_request.h"

#include <algorithm>
#include <cstring>

#include "q4t/server/websocket.h"
#include "q4t/test.h"

namespace {
using namespace q4t::server;
constexpr std::string_view kHead =
    "GET /v1/realtime?intent=transcription HTTP/1.1\r\nHost: local\r\n";
constexpr std::string_view kUpgrade =
    "Connection: UpGrade\r\nUpgrade: websocket\r\n"
    "Sec-WebSocket-Version: 13\r\n"
    "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n";
struct Parsed {
  HttpRequestHeaders headers;
  HttpRequestState state;
  std::string method, path, body;
  int calls = 0, status = 0;
  size_t read = 0;
  bool Run(std::string_view wire, size_t split = 65536,
           const std::function<bool()>& admission = {}) {
    return ReadHttpRequest(
        -1, &method, &path, &body, 1000, nullptr, &status, &headers,
        [&] { ++calls; return !admission || admission(); },
        [&](void* data, size_t size, auto) -> ssize_t {
          const auto count = std::min({size, split, wire.size() - read});
          if (count) std::memcpy(data, wire.data() + read, count);
          read += count;
          return count;
        }, &state);
  }
  WebSocketUpgrade Upgrade() const {
    return {method, headers.connection, headers.upgrade,
            headers.websocket_version, headers.websocket_key,
            headers.http11, headers.chunked, headers.content_length};
  }
};
}  // namespace

Q4T_TEST(http_upgrade_preserves_metadata_credentials_and_first_frame) {
  const auto wire = std::string(kHead) + std::string(kUpgrade) +
      "Connection: keep-alive\r\n"
      "Sec-WebSocket-Protocol: realtime, openai-insecure-api-key.AbC123\r\n"
      "Sec-WebSocket-Protocol: OtherCase\r\n\r\n" +
      std::string("\x81\x82\x01\x02\x03\x04\x7a\x7f", 8);
  Parsed p;
  Q4T_CHECK(p.Run(wire) && p.calls == 1 && p.body.empty());
  Q4T_CHECK(p.headers.connection == "upgrade,keep-alive");
  Q4T_CHECK(!p.headers.keep_alive && p.headers.http11);
  Q4T_CHECK(p.headers.websocket_protocols == std::vector<std::string>(
      {"realtime", "openai-insecure-api-key.AbC123", "OtherCase"}));
  Q4T_CHECK(p.state.pending == wire.substr(wire.size() - 8));
  std::string accept;
  Q4T_CHECK(CheckWebSocketUpgrade(p.Upgrade(), &accept) ==
            WebSocketUpgradeResult::kOk);
  Q4T_CHECK(accept == "s3pPLMBiTxaQ9kYGzzhZRbK+xOo=");
  return true;
}

Q4T_TEST(http_upgrade_header_splits_preserve_exact_unread_tail) {
  const auto wire = std::string(kHead) + std::string(kUpgrade) + "\r\nframe";
  for (const size_t split : {1, 2, 7, 16384}) {
    Parsed p;
    Q4T_CHECK(p.Run(wire, split) && p.body.empty() && p.calls == 1);
    Q4T_CHECK(p.state.pending + wire.substr(p.read) == "frame");
    Q4T_CHECK(p.headers.websocket_key == "dGhlIHNhbXBsZSBub25jZQ==");
  }
  return true;
}

Q4T_TEST(http_upgrade_rejects_duplicate_empty_and_oversized_singletons) {
  for (const auto name : {"Upgrade", "Sec-WebSocket-Key",
                          "Sec-WebSocket-Version"}) {
    for (const auto value : {"x", ""}) {
      Parsed p;
      Q4T_CHECK(!p.Run(std::string(kHead) + std::string(kUpgrade) + name +
                      ": " + value + "\r\n\r\n"));
      Q4T_CHECK(p.calls == 0 && p.headers.error_code ==
                                  "invalid_websocket_header");
    }
    Parsed p;
    Q4T_CHECK(!p.Run(std::string(kHead) + name + ": " +
                      std::string(257, 'x') + "\r\n\r\n"));
    Q4T_CHECK(p.calls == 0);
  }
  return true;
}

Q4T_TEST(http_upgrade_subprotocols_are_case_sensitive_unique_and_bounded) {
  const auto prefix = std::string(kHead) + std::string(kUpgrade);
  for (const auto value : {"", "realtime,", "a,,b", "\"quoted\"", "a b",
                           "a/a", "realtime,realtime", "a\t b"}) {
    Parsed p;
    Q4T_CHECK(!p.Run(prefix + "Sec-WebSocket-Protocol: " + value + "\r\n\r\n"));
    Q4T_CHECK(p.calls == 0);
  }
  Parsed good, repeated, large, excessive;
  Q4T_CHECK(good.Run(prefix + "Sec-WebSocket-Protocol: a,A\r\n\r\n"));
  Q4T_CHECK(good.headers.websocket_protocols ==
            std::vector<std::string>({"a", "A"}));
  Q4T_CHECK(!repeated.Run(prefix + "Sec-WebSocket-Protocol: a\r\n"
                                  "Sec-WebSocket-Protocol: a\r\n\r\n"));
  Q4T_CHECK(!large.Run(prefix + "Sec-WebSocket-Protocol: " +
                       std::string(4097, 'a') + "\r\n\r\n"));
  std::string protocols;
  for (int i = 0; i < 129; ++i) {
    if (i) protocols += ',';
    protocols += std::to_string(i);
  }
  Q4T_CHECK(!excessive.Run(prefix + "Sec-WebSocket-Protocol: " + protocols +
                            "\r\n\r\n"));
  return true;
}

Q4T_TEST(http_upgrade_security_fields_cannot_be_hop_options_or_trailers) {
  for (const auto field : {"Sec-WebSocket-Key", "Sec-WebSocket-Version",
                           "Sec-WebSocket-Protocol", "Sec-WebSocket-Extensions",
                           "Sec-WebSocket-Accept"}) {
    Parsed hop, declared, late;
    Q4T_CHECK(!hop.Run(std::string(kHead) + "Connection: " + field +
                       "\r\n\r\n"));
    Q4T_CHECK(!declared.Run(std::string(kHead) +
        "Transfer-Encoding: chunked\r\nTrailer: " + field +
        "\r\n\r\n0\r\n\r\n"));
    Q4T_CHECK(!late.Run(std::string(kHead) +
        "Transfer-Encoding: chunked\r\n\r\n0\r\n" + field + ": x\r\n\r\n"));
    Q4T_CHECK(hop.calls == 0 && declared.calls == 0);
  }
  return true;
}

Q4T_TEST(http_upgrade_admission_rejects_body_before_consumption) {
  for (const auto framing : {"Content-Length: 1\r\n",
                              "Transfer-Encoding: chunked\r\n"}) {
    Parsed p;
    const auto wire = std::string(kHead) + std::string(kUpgrade) + framing +
                       "Expect: 100-continue\r\n\r\n";
    std::string accept = "unchanged";
    Q4T_CHECK(!p.Run(wire, 1, [&] {
      return CheckWebSocketUpgrade(p.Upgrade(), &accept) ==
               WebSocketUpgradeResult::kOk;
    }));
    Q4T_CHECK(p.calls == 1 && p.body.empty() && accept == "unchanged");
    Q4T_CHECK(p.headers.failure == HttpReceiveFailure::kAdmission);
  }
  return true;
}

Q4T_TEST(http_upgrade_rejection_never_reuses_socket_for_frame_as_http) {
  Parsed upgrade, close, ordinary, old;
  Q4T_CHECK(upgrade.Run(std::string(kHead) + std::string(kUpgrade) + "\r\n"));
  Q4T_CHECK(!upgrade.headers.keep_alive);
  Q4T_CHECK(close.Run(std::string(kHead) + std::string(kUpgrade) +
                       "Connection: close\r\n\r\n"));
  std::string accept;
  Q4T_CHECK(CheckWebSocketUpgrade(close.Upgrade(), &accept) ==
            WebSocketUpgradeResult::kBadRequest);
  Q4T_CHECK(ordinary.Run(std::string(kHead) + "\r\n") &&
            ordinary.headers.keep_alive);
  Q4T_CHECK(old.Run("GET /v1/models HTTP/1.0\r\n"
                    "Connection: keep-alive\r\n\r\n") && old.headers.keep_alive);
  return true;
}
