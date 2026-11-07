// One CPU-only health projection for readiness, metrics and execution gates.
#pragma once

#include <string>

#include "q4t/runtime/model_health.h"
#include "q4t/server/memory_admission.h"

namespace q4t::server {

struct ServiceHealth {
  bool live = true;
  bool model_ready = false;
  bool gpu_healthy = true;
  bool draining = false;
  runtime::ModelFault model_fault = runtime::ModelFault::kNone;
  MemoryState memory = MemoryState::kDisabled;

  bool Healthy() const noexcept {
    return live && gpu_healthy && model_fault == runtime::ModelFault::kNone;
  }
  bool Ready() const noexcept {
    return Healthy() && model_ready && !draining &&
           (memory == MemoryState::kDisabled || memory == MemoryState::kOpen);
  }
  const char* State() const noexcept {
    if (!Healthy()) return "unhealthy";
    if (draining) return "draining";
    if (!model_ready) return "starting";
    if (memory == MemoryState::kLow) return "memory_pressure";
    if (memory == MemoryState::kUnavailable)
      return "memory_observation_unavailable";
    return "ready";
  }
  const char* ErrorCode() const noexcept {
    if (!gpu_healthy) return "gpu_unhealthy";
    if (model_fault != runtime::ModelFault::kNone) return "model_unhealthy";
    if (!live) return "server_unhealthy";
    return "";
  }
  std::string ReadinessJson() const {
    return std::string("{\"status\":\"") + State() +
           "\",\"live\":" + (live ? "true" : "false") +
           ",\"ready\":" + (Ready() ? "true" : "false") + "}";
  }
};

}  // namespace q4t::server
