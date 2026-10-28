#pragma once

// Opt-in bounded observation. CUDA events measure stream elapsed time, which
// includes submission gaps; they are not a sum of GPU kernel execution times.
// Flush only after an existing completion boundary. Never add a stream wait.
#include <cuda_runtime.h>
#include <time.h>

#include <array>
#include <cstdio>
#include <cstdlib>
#include <cstring>

namespace q4t::diagnostic {
inline bool PrefillTimingEnabled() {
  static const bool enabled = [] {
    const char* p = std::getenv("Q4T_PREFILL_SCORING_TIMING");
    return p && std::strcmp(p, "1") == 0;
  }();
  return enabled;
}
inline double PrefillClock(clockid_t clock) {
  timespec ts{};
  if (clock_gettime(clock, &ts) != 0) return -1;
  return ts.tv_sec * 1000.0 + ts.tv_nsec / 1e6;
}
struct PrefillTimingRecord {
  const char* label = nullptr;
  int position = -1, tokens = 0;
  cudaEvent_t begin = nullptr, end = nullptr;
  double wall = 0, cpu = 0;
  bool ok = true;
};
struct PrefillTimingBuffer {
  std::array<PrefillTimingRecord, 8> records{};
  int count = 0;
  bool overflow = false;
};
inline thread_local PrefillTimingBuffer prefill_timing;
class PrefillTimingSpan {
 public:
  PrefillTimingSpan(const char* label, int position, int tokens,
                    cudaStream_t stream = nullptr, bool eligible = true) {
    if (!eligible || !PrefillTimingEnabled()) return;
    if (prefill_timing.count == 8) {
      prefill_timing.overflow = true;
      return;
    }
    record_ = &prefill_timing.records[prefill_timing.count++];
    *record_ = {};
    record_->label = label;
    record_->position = position;
    record_->tokens = tokens;
    stream_ = stream;
    record_->ok = cudaEventCreate(&record_->begin) == cudaSuccess &&
                  cudaEventCreate(&record_->end) == cudaSuccess &&
                  cudaEventRecord(record_->begin, stream) == cudaSuccess;
    record_->wall = PrefillClock(CLOCK_MONOTONIC);
    record_->cpu = PrefillClock(CLOCK_THREAD_CPUTIME_ID);
    record_->ok &= record_->wall >= 0 && record_->cpu >= 0;
  }
  ~PrefillTimingSpan() { End(); }
  void End() {
    if (!record_) return;
    const double wall = PrefillClock(CLOCK_MONOTONIC);
    const double cpu = PrefillClock(CLOCK_THREAD_CPUTIME_ID);
    record_->ok &= wall >= 0 && cpu >= 0;
    record_->wall = wall - record_->wall;
    record_->cpu = cpu - record_->cpu;
    if (record_->ok)
      record_->ok = cudaEventRecord(record_->end, stream_) == cudaSuccess;
    record_ = nullptr;
  }
 private:
  PrefillTimingRecord* record_ = nullptr;
  cudaStream_t stream_ = nullptr;
};
inline void FlushPrefillTiming(const char* request) {
  if (!PrefillTimingEnabled()) return;
  if (prefill_timing.overflow)
    std::fprintf(stderr, "[prefill-score-timing] error=overflow\n");
  for (int i = 0; i < prefill_timing.count; ++i) {
    auto& r = prefill_timing.records[i];
    float gpu_ms = -1;
    const bool ok = r.ok && cudaEventQuery(r.end) == cudaSuccess &&
        cudaEventElapsedTime(&gpu_ms, r.begin, r.end) == cudaSuccess;
    std::fprintf(stderr,
        "[prefill-score-timing] request=%s label=%s position=%d tokens=%d "
        "wall_ms=%.6f cpu_ms=%.6f stream_ms=%.6f ok=%d\n",
        request, r.label, r.position, r.tokens, r.wall, r.cpu, gpu_ms, ok);
    if (r.begin) cudaEventDestroy(r.begin);
    if (r.end) cudaEventDestroy(r.end);
  }
  prefill_timing.count = 0;
  prefill_timing.overflow = false;
}
}  // namespace q4t::diagnostic
