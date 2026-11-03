#pragma once

#include <array>
#include <chrono>
#include <cstdint>
#include <mutex>

#include "q4t/runtime/system_memory.h"

namespace q4t::server {

enum class MemoryState { kDisabled, kOpen, kLow, kUnavailable };
inline constexpr std::array<const char*, 4> kMemoryStates = {
    "disabled", "open", "memory_pressure", "memory_observation_unavailable"};
enum class MemoryCheckpoint { kHeaders, kPrepare, kExecute };
inline constexpr std::array<const char*, 3> kMemoryCheckpoints = {
    "headers", "prepare", "execute"};
inline constexpr std::array<const char*, 2> kMemoryRejectionReasons = {
    "memory_pressure", "memory_observation_unavailable"};
inline constexpr int kMemoryObservationIntervalMs = 250;

struct MemoryAdmissionSnapshot {
  MemoryState state = MemoryState::kDisabled;
  runtime::SystemMemory memory;
  uint64_t floor_bytes = 0;
  uint64_t recovery_bytes = 0;
  uint64_t observations = 0;
  uint64_t failures = 0;
  uint64_t transitions = 0;
  bool Allowed() const {
    return state == MemoryState::kDisabled || state == MemoryState::kOpen;
  }
  const char* StateName() const {
    return kMemoryStates[static_cast<size_t>(state)];
  }
};

// Advisory global-memory gate; does not reserve bytes or cancel active work.
// Configure before serving. Reader/clock seams are internal CPU test inputs;
// production configuration never accepts an alternate observation source.
class MemoryAdmission {
 public:
  using Clock = std::chrono::steady_clock;
  using Reader = runtime::SystemMemory (*)();
  using Now = Clock::time_point (*)();
  void Configure(uint64_t floor, uint64_t recovery_margin,
                 Reader reader = runtime::ReadSystemMemory,
                 Now now = Clock::now);
  MemoryAdmissionSnapshot Observe(bool refresh = false);

 private:
  std::mutex mutex_;
  Reader reader_ = runtime::ReadSystemMemory;
  Now now_ = Clock::now;
  Clock::time_point sampled_{};
  bool sampled_once_ = false;
  MemoryAdmissionSnapshot snapshot_;
};

}  // namespace q4t::server
