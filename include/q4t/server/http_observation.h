// Fixed transport reasons. These counts are not chat task outcomes, and labels
// never contain caller text, paths, request IDs or errno strings.
#pragma once

#include <array>
#include <atomic>
#include <cstddef>
#include <cstdint>

namespace q4t::server {

enum class HttpReceiveFailure {
  kNone,
  kMalformed,
  kHeaderTooLarge,
  kBodyTooLarge,
  kBodyCapacity,
  kHeaderDeadline,
  kBodyDeadline,
  kHeaderEof,
  kBodyEof,
  kSocket,
  kAdmission,
  kConnectionCapacity,
  kDraining,
  kMedia,
  kMethod,
  kQuery,
  kUnexpectedBody,
  kRoute,
  kException,
  kBodyConsumer,
  kCount
};
inline constexpr std::array kHttpReceiveReasons = {"none",
                                                   "malformed",
                                                   "header_too_large",
                                                   "body_too_large",
                                                   "body_capacity",
                                                   "header_deadline",
                                                   "body_deadline",
                                                   "header_eof",
                                                   "body_eof",
                                                   "socket_error",
                                                   "admission",
                                                   "connection_capacity",
                                                   "server_draining",
                                                   "unsupported_media",
                                                   "method",
                                                   "query",
                                                   "unexpected_body",
                                                   "route",
                                                   "exception",
                                                   "body_consumer"};
static_assert(kHttpReceiveReasons.size() ==
              static_cast<size_t>(HttpReceiveFailure::kCount));

enum class HttpWriteFailure {
  kNone,
  kWriteDeadline,
  kRequestDeadline,
  kExplicitCancel,
  kPeerClosed,
  kShutdown,
  kRequestFailure,
  kSocket,
  kException,
  kCount
};
inline constexpr std::array kHttpWriteReasons = {
    "none",        "write_deadline",  "request_deadline", "explicit_cancel",
    "peer_closed", "server_shutdown", "request_failure",  "socket_error",
    "exception"};
static_assert(kHttpWriteReasons.size() ==
              static_cast<size_t>(HttpWriteFailure::kCount));

enum class HttpWriteOrigin { kHandler, kHeartbeat, kCount };
inline constexpr std::array kHttpWriteOrigins = {"handler", "heartbeat"};
static_assert(kHttpWriteOrigins.size() ==
              static_cast<size_t>(HttpWriteOrigin::kCount));

struct HttpWriteObservation {
  HttpWriteFailure reason = HttpWriteFailure::kNone;
  HttpWriteOrigin origin = HttpWriteOrigin::kHandler;
  // Bytes sent during the failing logical write, not cumulative connection
  // bytes or a promise that the peer consumed them.
  size_t transferred = 0;
  int system_error = 0;
  short poll_events = 0;
};

struct HttpTransportMetrics {
  std::array<std::atomic<uint64_t>, kHttpReceiveReasons.size()> receive{};
  std::array<std::array<std::atomic<uint64_t>, kHttpWriteReasons.size()>,
             kHttpWriteOrigins.size()>
      write{};
};

}  // namespace q4t::server
