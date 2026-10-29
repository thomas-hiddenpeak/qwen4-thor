#pragma once

#include <time.h>

#include <array>
#include <cerrno>
#include <chrono>
#include <cstdio>
#include <cstdlib>
#include <cstring>

namespace q4t::server {

// Opt-in host observation. Copy wall time includes CUDA/driver waiting.
class SessionSaveTiming {
 public:
  enum Stage {
    kModelLock,
    kRanges,
    kCacheLock,
    kMetadata,
    kOpen,
    kCopy,
    kPadding,
    kHash,
    kWrite,
    kPublish,
    kCleanup,
    kCount
  };
  static bool Enabled() {
    static const bool enabled = [] {
      const char* value = std::getenv("Q4T_SESSION_SAVE_TIMING");
      return value && std::strcmp(value, "1") == 0;
    }();
    return enabled;
  }
  static double Wall() {
    return std::chrono::duration<double, std::milli>(
               std::chrono::steady_clock::now().time_since_epoch())
        .count();
  }
  double Cpu() {
    timespec value{};
    if (clock_gettime(CLOCK_THREAD_CPUTIME_ID, &value)) valid = false;
    return value.tv_sec * 1000.0 + value.tv_nsec / 1000000.0;
  }
  SessionSaveTiming(const char* request, const char* pool)
      : request_(request), pool_(pool) {
    if (Enabled()) {
      start_wall_ = Wall();
      start_cpu_ = Cpu();
    }
  }
  class Scope {
   public:
    Scope(SessionSaveTiming* timing, Stage stage)
        : timing_(Enabled() ? timing : nullptr), stage_(stage) {
      const int error = errno;
      if (timing_) {
        wall_ = Wall();
        cpu_ = timing_->Cpu();
      }
      errno = error;
    }
    ~Scope() {
      if (!timing_) return;
      const int error = errno;
      const double elapsed = Wall() - wall_;
      timing_->wall_[stage_] += elapsed;
      timing_->cpu_[stage_] += timing_->Cpu() - cpu_;
      ++timing_->calls_[stage_];
      if (stage_ == kWrite) {
        if (elapsed > timing_->max_write_ms) timing_->max_write_ms = elapsed;
        if (elapsed > 50) ++timing_->slow_writes;
      }
      errno = error;
    }
    Scope(const Scope&) = delete;
    Scope& operator=(const Scope&) = delete;

   private:
    SessionSaveTiming* timing_;
    Stage stage_;
    double wall_ = 0, cpu_ = 0;
  };
  void Report(bool success) {
    if (!Enabled()) return;
    const double wall = Wall() - start_wall_;
    const double cpu = Cpu() - start_cpu_;
    constexpr const char* names[] = {"model_lock", "ranges", "cache_lock",
                                     "metadata", "open", "copy_api", "padding",
                                     "sha256", "pwrite", "publish", "cleanup"};
    double measured_wall = 0, measured_cpu = 0;
    flockfile(stderr);
    std::fprintf(
        stderr,
        "[session-save-timing] request=%s pool=%s success=%d valid=%d "
        "wall_ms=%.3f cpu_ms=%.3f data_bytes=%zu disk_bytes=%zu "
        "max_write_ms=%.3f slow_writes=%zu "
        "incremental=%d reused_bytes=%zu reused_blocks=%zu "
        "scanned_bytes=%zu file_span=%zu allocated_bytes=%zu",
        request_, pool_, success, valid, wall, cpu, data_bytes, disk_bytes,
        max_write_ms, slow_writes, incremental, reused_bytes,
        reused_blocks, scanned_bytes, file_span, allocated_bytes);
    for (int i = 0; i < kCount; ++i) {
      measured_wall += wall_[i];
      measured_cpu += cpu_[i];
      std::fprintf(stderr, " %s_ms=%.3f %s_cpu_ms=%.3f %s_calls=%zu", names[i],
                   wall_[i], names[i], cpu_[i], names[i], calls_[i]);
    }
    std::fprintf(stderr, " residual_ms=%.3f residual_cpu_ms=%.3f\n",
                 wall - measured_wall, cpu - measured_cpu);
    funlockfile(stderr);
  }
  bool valid = true;
  size_t data_bytes = 0, disk_bytes = 0, slow_writes = 0;
  double max_write_ms = 0;
  bool incremental = false;
  size_t reused_bytes = 0, reused_blocks = 0, scanned_bytes = 0;
  size_t file_span = 0, allocated_bytes = 0;

 private:
  const char* request_;
  const char* pool_;
  double start_wall_ = 0, start_cpu_ = 0;
  std::array<double, kCount> wall_{}, cpu_{};
  std::array<size_t, kCount> calls_{};
};

}  // namespace q4t::server
