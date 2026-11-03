// Snapshot admission before model allocation; not a reservation or hard limit.
#pragma once

#include <cstdint>
#include <limits>
#include <string>

#include "q4t/runtime/system_memory.h"

namespace q4t::server {

enum class StartupMemoryState {
  kNotObserved,
  kBudgetDisabled,
  kDisabled,
  kEstimateUnavailable,
  kRequiredOverflow,
  kObservationUnavailable,
  kInsufficient,
  kAdmitted
};

struct StartupMemorySnapshot {
  StartupMemoryState state = StartupMemoryState::kNotObserved;
  runtime::SystemMemory memory;
  uint64_t estimate_bytes = 0;
  uint64_t headroom_bytes = 0;
  uint64_t required_bytes = 0;

  bool Allowed() const {
    return state == StartupMemoryState::kBudgetDisabled ||
           state == StartupMemoryState::kDisabled ||
           state == StartupMemoryState::kAdmitted;
  }
  const char* Reason() const {
    switch (state) {
      case StartupMemoryState::kNotObserved: return "not_observed";
      case StartupMemoryState::kBudgetDisabled: return "budget_disabled";
      case StartupMemoryState::kDisabled: return "disabled";
      case StartupMemoryState::kEstimateUnavailable:
        return "startup_memory_estimate_unavailable";
      case StartupMemoryState::kRequiredOverflow:
        return "startup_memory_required_overflow";
      case StartupMemoryState::kObservationUnavailable:
        return "startup_memory_observation_unavailable";
      case StartupMemoryState::kInsufficient:
        return "startup_memory_insufficient";
      case StartupMemoryState::kAdmitted: return "admitted";
    }
    return "not_observed";
  }
};

// A disabled budget has no usable estimate. It explicitly bypasses this check
// as well as sizing; runtime request memory admission remains independent.
inline StartupMemorySnapshot ObserveStartupMemory(
    bool budget_enabled, uint64_t estimate_bytes, uint64_t headroom_bytes,
    bool require_estimate = true,
    runtime::SystemMemory (*read)() = runtime::ReadSystemMemory) {
  StartupMemorySnapshot result;
  result.estimate_bytes = estimate_bytes;
  result.headroom_bytes = headroom_bytes;
  if (!budget_enabled) {
    result.state = StartupMemoryState::kBudgetDisabled;
  } else if (headroom_bytes == 0) {
    result.state = StartupMemoryState::kDisabled;
  } else if (require_estimate && estimate_bytes == 0) {
    result.state = StartupMemoryState::kEstimateUnavailable;
  } else if (estimate_bytes >
             std::numeric_limits<uint64_t>::max() - headroom_bytes) {
    result.state = StartupMemoryState::kRequiredOverflow;
  } else {
    result.required_bytes = estimate_bytes + headroom_bytes;
    result.memory = read ? read() : runtime::SystemMemory{};
    if (!result.memory.valid || result.memory.total_bytes == 0 ||
        result.memory.available_bytes > result.memory.total_bytes) {
      result.memory = {};
      result.state = StartupMemoryState::kObservationUnavailable;
    } else {
      result.state = result.memory.available_bytes >= result.required_bytes
                         ? StartupMemoryState::kAdmitted
                         : StartupMemoryState::kInsufficient;
    }
  }
  return result;
}

inline std::string StartupMemorySnapshotJson(
    const StartupMemorySnapshot& value) {
  return std::string(R"({"state":")") + value.Reason() +
         R"(","estimate_bytes":)" +
         (value.estimate_bytes ? std::to_string(value.estimate_bytes) : "null") +
         R"(,"headroom_bytes":)" + std::to_string(value.headroom_bytes) +
         R"(,"required_bytes":)" +
         (value.required_bytes ? std::to_string(value.required_bytes) : "null") +
         R"(,"observed_total_bytes":)" +
         (value.memory.valid ? std::to_string(value.memory.total_bytes)
                             : "null") +
         R"(,"observed_available_bytes":)" +
         (value.memory.valid ? std::to_string(value.memory.available_bytes)
                             : "null") + '}';
}

struct StartupMemoryChecks {
  StartupMemorySnapshot before_metadata;
  StartupMemorySnapshot before_allocations;
  StartupMemorySnapshot before_ready;
};

inline std::string StartupMemoryChecksJson(const StartupMemoryChecks& checks) {
  return R"({"before_metadata":)" +
         StartupMemorySnapshotJson(checks.before_metadata) +
         R"(,"before_allocations":)" +
         StartupMemorySnapshotJson(checks.before_allocations) +
         R"(,"before_ready":)" +
         StartupMemorySnapshotJson(checks.before_ready) + '}';
}

}  // namespace q4t::server
