// CPU-side resource and health contract for the optional native ASR backend.
#pragma once

#include <chrono>
#include <cstddef>
#include <cstdint>
#include <mutex>
#include <string>
#include <string_view>

#include "q4t/status.h"

namespace q4t::server {
struct AsrServiceLimits {
  int max_audio_seconds = 30;
  int max_context_tokens = 4096;
  int max_output_tokens = 1024;
  int workspace_mib = 1024;
  bool Valid() const {
    return max_audio_seconds >= 1 && max_audio_seconds <= 600 &&
           max_context_tokens >= 128 && max_context_tokens <= 8192 &&
           max_output_tokens >= 1 && max_output_tokens <= 4096 &&
           workspace_mib >= 64 && workspace_mib <= 8192 &&
           max_audio_seconds * 13 + 15 + max_output_tokens <=
               max_context_tokens;
  }
};
struct AsrResourcePlan {
  uint64_t encoder_bytes = 0;
  uint64_t decoder_bytes = 0;
  uint64_t request_device_bytes = 0;
  uint64_t request_host_bytes = 0;
  uint64_t shared_host_bytes = 0;
  uint64_t load_peak_bytes = 0;
};
// Estimates include direct-read host staging and device payload in
// the same physical RAM. Not a reservation or a guarantee against host OOM.
Status PlanAsrServiceResources(const AsrServiceLimits& limits,
                               AsrResourcePlan* out, size_t batch_workers = 1);
std::string AsrServiceLimitsJson(const AsrServiceLimits& limits);

enum class AsrFailure {
  kNone,
  kAssets,
  kTimeout,
  kWorker,
  kResource,
  kDevice,
  kCompute
};
enum class AsrLoadAdmission {
  kDisabled,
  kReady,
  kStart,
  kLoading,
  kCooldown,
  kDevice
};
struct AsrHealthSnapshot {
  bool configured = false;
  bool loading = false;
  bool metadata_ready = false;
  bool ready = false;
  uint64_t device_bytes = 0;
  uint64_t load_attempts = 0;
  uint64_t load_failures = 0;
  uint64_t compute_failures = 0;
  AsrFailure failure = AsrFailure::kNone;
  std::chrono::steady_clock::time_point retry_after{};
};

// This mutex is never held across asset I/O, GPU locking or callbacks. The
// server separately serializes loading and publishes immutable weights.
// A device fault is sticky in this process; request retries cannot clear it.
class AsrHealth {
 public:
  using Clock = std::chrono::steady_clock;
  static constexpr auto kRetryDelay = std::chrono::seconds(5);
  void Configure(bool configured);
  AsrLoadAdmission BeginLoad(bool weights, Clock::time_point now);
  void Loaded(bool weights, uint64_t device_bytes);
  void CancelLoad();
  void LoadFailed(AsrFailure failure, Clock::time_point now);
  void ComputeFailed(AsrFailure failure, Clock::time_point now);
  void ComputeSucceeded();
  void Released();
  AsrHealthSnapshot Snapshot() const;

 private:
  mutable std::mutex mutex_;
  AsrHealthSnapshot state_;
};
const char* AsrFailureCode(AsrFailure failure);
std::string AsrHealthJson(const AsrHealthSnapshot& state, bool gpu_healthy);
// Shared serialization only. Each optional backend owns a distinct AsrHealth
// instance; dependency failures cannot mutate another backend's readiness.
std::string SpeechHealthJson(const AsrHealthSnapshot& state, bool gpu_healthy,
                             std::string_view model,
                             const char* (*failure_code)(AsrFailure));
}  // namespace q4t::server
