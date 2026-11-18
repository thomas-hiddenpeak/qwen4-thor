// Bounded opaque duplex transport after an authenticated HTTP upgrade.
#pragma once

#include <chrono>
#include <cstddef>
#include <cstdint>
#include <string_view>

#include "q4t/service/ingress_transport.h"

namespace q4t::service::ingress {
inline constexpr size_t kDuplexBufferBytes = 65536;
struct DuplexLimits {
  std::chrono::milliseconds idle{60000};
  std::chrono::milliseconds stalled_write{15000};
  std::chrono::milliseconds session{3600000};
  uint64_t bytes_per_direction = 8ULL << 30;
};
enum class DuplexEnd {
  kClientEof,
  kUpstreamEof,
  kIdleDeadline,
  kWriteDeadline,
  kSessionDeadline,
  kByteLimit,
  kStopped,
  kIoError,
  kInvalidLimits,
};
struct DuplexResult {
  DuplexEnd reason = DuplexEnd::kInvalidLimits;
  uint64_t client_received = 0, upstream_received = 0;
  uint64_t client_sent = 0, upstream_sent = 0;
  size_t peak_to_client = 0, peak_to_upstream = 0;
  int system_error = 0;
};
// The owning event-loop thread alone touches client (including its TLS state).
// Prefixes are unread bytes after the HTTP headers, copied before any I/O.
// Exactly two fixed 64-KiB buffers; neither is refilled until fully written.
// Independent write deadlines prevent other-direction traffic hiding a stuck
// consumer. EOF is a transport result, NEVER a successful WebSocket close.
// No retries/replay/reconnect, model operations or closing of borrowed fds.
DuplexResult RelayDuplex(int upstream, Connection& client,
                         std::string_view client_prefix,
                         std::string_view upstream_prefix,
                         const DuplexLimits& limits = {});
}  // namespace q4t::service::ingress
