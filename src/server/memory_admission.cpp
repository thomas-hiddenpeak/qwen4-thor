#include "q4t/server/memory_admission.h"

#include <algorithm>
#include <limits>

namespace q4t::server {

void MemoryAdmission::Configure(uint64_t floor, uint64_t recovery_margin,
                                Reader reader, Now now) {
  const std::lock_guard<std::mutex> lock(mutex_);
  reader_ = reader;
  now_ = now;
  sampled_once_ = false;
  snapshot_ = {};
  snapshot_.floor_bytes = floor;
  snapshot_.recovery_bytes =
      floor ? floor + std::min(recovery_margin,
                              std::numeric_limits<uint64_t>::max() - floor)
            : 0;
  snapshot_.state = floor ? MemoryState::kUnavailable : MemoryState::kDisabled;
}

MemoryAdmissionSnapshot MemoryAdmission::Observe(bool refresh) {
  const std::lock_guard<std::mutex> lock(mutex_);
  if (snapshot_.state == MemoryState::kDisabled) return snapshot_;
  const auto now = now_();
  if (!refresh && sampled_once_ && now >= sampled_ &&
      now - sampled_ < std::chrono::milliseconds(kMemoryObservationIntervalMs))
    return snapshot_;
  snapshot_.memory = reader_();
  sampled_ = now;
  sampled_once_ = true;
  ++snapshot_.observations;
  const auto old = snapshot_.state;
  if (!snapshot_.memory.valid) {
    snapshot_.memory = {};
    snapshot_.state = MemoryState::kUnavailable;
    ++snapshot_.failures;
  } else {
    const uint64_t threshold = old == MemoryState::kOpen
                                   ? snapshot_.floor_bytes
                                   : snapshot_.recovery_bytes;
    snapshot_.state = snapshot_.memory.available_bytes >= threshold
                          ? MemoryState::kOpen
                          : MemoryState::kLow;
  }
  if (old != snapshot_.state) ++snapshot_.transitions;
  return snapshot_;
}

}  // namespace q4t::server
