// Bounded connection partitions after exact HTTP method/path classification.
#pragma once

#include <array>
#include <atomic>
#include <string_view>

#include "q4t/server/model_catalog.h"
#include "q4t/server/application_routes.h"

namespace q4t::server {

inline constexpr int kReservedControlConnections = 4;
inline constexpr int kReservedObservationConnections = 4;
inline constexpr int kTunnelPollConnections = 4;
inline constexpr int kTunnelResponseConnections = 4;
inline constexpr int kRealtimeConnections = 4;
inline constexpr int kSidebandConnections = 4;
inline constexpr int kOperationsConnections =
    kReservedControlConnections + kReservedObservationConnections;

enum class HttpConnectionClass {
  kUnclassified,
  kBusiness,
  kObservation,
  kControl,
  kTunnelPoll,
  kTunnelResponse,
  kRealtime,
  kSideband
};
inline constexpr std::array<std::string_view, 8> kHttpConnectionClasses = {
    "unclassified", "business",        "observation", "control",
    "tunnel_poll",  "tunnel_response", "realtime", "live_sideband"};

inline HttpConnectionClass ClassifyHttpConnection(std::string_view method,
                                                  std::string_view path) {
  if ((method == "GET" && IsCallSideband(path)) || IsAgentEventStream(method, path))
    return HttpConnectionClass::kSideband;
  if (IsAgentEventSubmission(method, path)) return HttpConnectionClass::kControl;
  if (IsRealtimeUpgrade(method, path) || IsLiveMediaCreate(method, path)) return HttpConnectionClass::kRealtime;
  if (IsMcpTunnelPoll(method, path)) return HttpConnectionClass::kTunnelPoll;
  if (IsMcpTunnelResponse(method, path))
    return HttpConnectionClass::kTunnelResponse;
  if ((method == "GET" &&
       (path == "/healthz" || path == "/livez" || path == "/readyz")) ||
      IsCancellationRequest(method, path) || (IsSipReject(method, path) || IsSipHangup(method, path) || IsSipRefer(method, path)))
    return HttpConnectionClass::kControl;
  if (method == "GET" && (path == "/metrics" || path == "/v1/config" ||
                          path == "/v1/capabilities" || path == "/v1/models" ||
                          IsModelDetailPath(path)))
    return HttpConnectionClass::kObservation;
  if (method == "DELETE" && IsModelDetailPath(path))
    return HttpConnectionClass::kControl;
  if (IsApplicationObservation(method, path))
    return HttpConnectionClass::kObservation;
  return HttpConnectionClass::kBusiness;
}

// A refused acquire never transiently publishes an over-limit owner count.
inline bool AcquireHttpConnection(std::atomic<int>& count, int limit) {
  int old = count.load(std::memory_order_relaxed);
  do {
    if (old >= limit) return false;
  } while (
      !count.compare_exchange_weak(old, old + 1, std::memory_order_relaxed));
  return true;
}

}  // namespace q4t::server
