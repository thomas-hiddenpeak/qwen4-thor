// Optional host wall-clock diagnostics. No CUDA calls, allocation or locking.
// Stages must not overlap. The caller owns the request-local accumulator.
#pragma once

#include <array>
#include <chrono>
#include <cstdio>
#include <cstdlib>
#include <cstring>

namespace q4t::vision {

enum class TimingStage {
  kPrepare,
  kDecodePng,
  kDecodeJpeg,
  kResize,
  kNormalize,
  kPatchify,
  kPack,
  kPixelsAllocate,
  kPixelsUpload,
  kWorkspace,
  kOutputAllocate,
  kForward,
  kCleanup,
  kCount
};

inline constexpr std::array<const char*,
                            static_cast<size_t>(TimingStage::kCount)>
    kTimingNames{"prepare",   "decode_png",      "decode_jpeg",
                 "resize",    "normalize",       "patchify",
                 "pack",      "pixels_allocate", "pixels_upload",
                 "workspace", "output_allocate", "forward",
                 "cleanup"};

inline bool VisionTimingEnabled() {
  // Environment is fixed at process startup, as with other diagnostic flags.
  static const bool enabled = [] {
    const char* value = std::getenv("Q4T_VISION_TIMING");
    return value && std::strcmp(value, "1") == 0;
  }();
  return enabled;
}

class StageTimings {
 public:
  using Clock = std::chrono::steady_clock;
  explicit StageTimings(bool enabled)
      : enabled_(enabled),
        start_(enabled ? Clock::now() : Clock::time_point{}) {}

  StageTimings* Active() { return enabled_ ? this : nullptr; }
  void Observe(TimingStage stage, Clock::duration elapsed) {
    const size_t index = static_cast<size_t>(stage);
    milliseconds_[index] +=
        std::chrono::duration<double, std::milli>(elapsed).count();
    ++calls_[index];
  }
  double Milliseconds(TimingStage stage) const {
    return milliseconds_[static_cast<size_t>(stage)];
  }
  unsigned long long Calls(TimingStage stage) const {
    return calls_[static_cast<size_t>(stage)];
  }
  void Report(FILE* sink, const char* id, bool completed, size_t items,
              size_t frames, unsigned long long patches) const {
    if (!enabled_) return;
    const double total =
        std::chrono::duration<double, std::milli>(Clock::now() - start_)
            .count();
    double measured = 0;
    char fields[2048];
    size_t offset = 0;
    for (size_t i = 0; i < kTimingNames.size(); ++i) {
      measured += milliseconds_[i];
      const int n = std::snprintf(fields + offset, sizeof(fields) - offset,
                                  " %s_ms=%.6f %s_calls=%llu", kTimingNames[i],
                                  milliseconds_[i], kTimingNames[i], calls_[i]);
      if (n < 0 || static_cast<size_t>(n) >= sizeof(fields) - offset) return;
      offset += static_cast<size_t>(n);
    }
    // One stdio operation prevents interleaving with other request log lines.
    // Negative residual is deliberately not clamped: analysis must reject an
    // overlapping measurement instead of hiding it. Not pure GPU/kernel time.
    std::fprintf(sink,
                 "[q4t][vision-timing] version=1 id=%s clock=host_wall "
                 "completed=%d items=%zu frames=%zu patches=%llu "
                 "total_ms=%.6f other_ms=%.6f%s\n",
                 id, completed, items, frames, patches, total, total - measured,
                 fields);
  }

 private:
  const bool enabled_;
  const Clock::time_point start_;
  std::array<double, kTimingNames.size()> milliseconds_{};
  std::array<unsigned long long, kTimingNames.size()> calls_{};
};

class TimingScope {
 public:
  TimingScope(StageTimings* timings, TimingStage stage)
      : timings_(timings),
        stage_(stage),
        start_(timings ? StageTimings::Clock::now()
                       : StageTimings::Clock::time_point{}) {}
  ~TimingScope() { Stop(); }
  TimingScope(const TimingScope&) = delete;
  TimingScope& operator=(const TimingScope&) = delete;
  void Stop() {
    if (!timings_) return;
    timings_->Observe(stage_, StageTimings::Clock::now() - start_);
    timings_ = nullptr;
  }

 private:
  StageTimings* timings_;
  const TimingStage stage_;
  const StageTimings::Clock::time_point start_;
};

}  // namespace q4t::vision
