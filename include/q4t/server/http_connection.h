// Bounded connection partitions after exact HTTP method/path classification.
#pragma once

#include <array>
#include <atomic>
#include <string_view>

namespace q4t::server {

inline constexpr int kReservedControlConnections = 4;
inline constexpr int kReservedObservationConnections = 4;

enum class HttpConnectionClass {
  kUnclassified,
  kBusiness,
  kObservation,
  kControl
};
inline constexpr std::array<std::string_view, 4> kHttpConnectionClasses = {
    "unclassified", "business", "observation", "control"};

inline HttpConnectionClass ClassifyHttpConnection(std::string_view method,
                                                  std::string_view path) {
  if ((method == "GET" &&
       (path == "/healthz" || path == "/livez" || path == "/readyz")) ||
      (method == "POST" && path == "/v1/requests/cancel"))
    return HttpConnectionClass::kControl;
  if (method == "GET" && (path == "/metrics" || path == "/v1/config" ||
                          path == "/v1/capabilities" || path == "/v1/models"))
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
