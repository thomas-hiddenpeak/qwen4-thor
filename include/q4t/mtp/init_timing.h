#pragma once

#include <cuda_runtime_api.h>

#include <array>
#include <chrono>
#include <cstddef>
#include <string>
#include <vector>

namespace q4t::mtp {

// Request-local diagnostic only. Events never synchronize the stream. Adjacent
// markers describe stream elapsed time, including host submission gaps; these
// durations are not GPU-active time and cannot be added to host wall time.
enum class InitGpuPoint {
  kInputBegin,
  kSetupBegin,
  kProjectionsBegin,
  kAttentionHcBegin,
  kAttentionBegin,
  kMlpHcBegin,
  kMoeBegin,
  kMixerBegin,
  kHeadBegin,
  kEnd,
};
enum class InitOuterPoint {
  kPrefillBegin,
  kPrefillEnd,
  kResetBegin,
  kResetEnd,
};

class MtpInitTiming {
 public:
  using Clock = std::chrono::steady_clock;
  MtpInitTiming(std::string response_id, int rows, int chunk_size,
                size_t trunk_bytes, Clock::time_point origin);
  ~MtpInitTiming() noexcept;
  MtpInitTiming(const MtpInitTiming&) = delete;
  MtpInitTiming& operator=(const MtpInitTiming&) = delete;

  void MarkHost(const char* name, int base = -1, int rows = 0) noexcept;
  void MarkOuter(InitOuterPoint point, cudaStream_t stream) noexcept;
  void BeginChunk(int base, int rows, bool compute_logits, bool last_row,
                  cudaStream_t stream, bool tail_skipped = false) noexcept;
  void MarkGpu(InitGpuPoint point, cudaStream_t stream) noexcept;
  void FinishInit(bool success) noexcept { init_completed_ = success; }
  bool FirstContentWritten() const noexcept { return first_content_written_; }
  void FinishFirstContent() noexcept;

 private:
  static constexpr size_t kPoints = 10;
  static constexpr size_t kMaxChunks = 64;
  static constexpr size_t kMaxHostMarks = 256;
  struct Event {
    cudaEvent_t handle = nullptr;
    double host_ms = -1;
    bool recorded = false;
  };
  struct Chunk {
    int base = 0;
    int rows = 0;
    bool compute_logits = false;
    bool last_row = false;
    bool tail_skipped = false;
    size_t next_point = 0;
    std::array<Event, kPoints> events;
  };
  struct HostMark {
    const char* name = nullptr;
    double at_ms = 0;
    int base = -1;
    int rows = 0;
  };
  void Fail(const char* reason) noexcept;
  void Create(Event& event) noexcept;
  void Record(Event& event, cudaStream_t stream) noexcept;
  void Report();
  double NowMs() const noexcept;

  std::string response_id_;
  int rows_;
  int chunk_size_;
  size_t trunk_bytes_;
  Clock::time_point origin_;
  std::vector<Chunk> chunks_;
  std::array<Event, 4> outer_;
  std::array<HostMark, kMaxHostMarks> host_;
  size_t chunk_count_ = 0;
  size_t host_count_ = 0;
  const char* error_ = nullptr;
  bool init_completed_ = false;
  bool first_content_written_ = false;
  double setup_host_ms_ = 0;
  double record_host_ms_ = 0;
};

}  // namespace q4t::mtp
