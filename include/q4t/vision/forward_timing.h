// Opt-in vision event diagnostics. No added synchronization or error clearing.
#pragma once

#include <cuda_runtime.h>

#include <chrono>
#include <cstdlib>
#include <cstring>

#include "q4t/runtime/cleanup_policy.h"
#include "q4t/vision/forward_timing_core.h"

namespace q4t::vision {

inline bool VisionForwardTimingEnabled() {
  static const bool enabled = [] {
    const char* value = std::getenv("Q4T_VISION_FORWARD_TIMING");
    return value && std::strcmp(value, "1") == 0;
  }();
  return enabled;
}

struct ForwardCudaEvents {
  using Event = cudaEvent_t;
  using Stream = cudaStream_t;
  int Create(Event* event) { return cudaEventCreate(event); }
  int Record(Event event, Stream stream) {
    return cudaEventRecord(event, stream);
  }
  int Elapsed(float* ms, Event begin, Event end) {
    return cudaEventElapsedTime(ms, begin, end);
  }
  void Destroy(Event event) {
    runtime::RequireCleanupSuccess(cudaEventDestroy(event), "cudaEventDestroy");
  }
};

class ForwardTiming : public ForwardEventTiming<ForwardCudaEvents> {
 public:
  using Clock = std::chrono::steady_clock;
  explicit ForwardTiming(bool enabled) : ForwardEventTiming(enabled) {}
  ForwardTiming* Active() { return Enabled() ? this : nullptr; }
  bool Prepare(size_t items, int depth) {
    if (!Enabled()) return true;
    const auto start = Clock::now();
    const bool ok = ForwardEventTiming::Prepare(items, depth);
    setup_ms_ = MillisecondsSince(start);
    return ok;
  }
  bool CollectAfterCompletion() {
    if (!Enabled()) return true;
    const auto start = Clock::now();
    const bool ok = ForwardEventTiming::CollectAfterCompletion();
    read_ms_ = MillisecondsSince(start);
    return ok;
  }
  void AddPositionHost(Clock::time_point start) {
    position_ms_ += MillisecondsSince(start);
  }
  void Report(FILE* sink, const char* id, size_t items, size_t patches,
              int depth) const {
    ForwardEventTiming::Report(sink, id, items, patches, depth, setup_ms_,
                               read_ms_, position_ms_);
  }

 private:
  static double MillisecondsSince(Clock::time_point start) {
    return std::chrono::duration<double, std::milli>(Clock::now() - start)
        .count();
  }
  double setup_ms_ = 0;
  double read_ms_ = 0;
  double position_ms_ = 0;
};

}  // namespace q4t::vision
