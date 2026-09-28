// Controlled full-request capture; callers retain the existing stream wait.
#pragma once
#include <cuda_runtime.h>
#include <atomic>
#include <array>
#include <memory>
#include <string>
#include <thread>
#include "q4t/trace/router_trace.h"

namespace q4t::trace {
class RouterCollector {
 public:
  RouterCollector() = default;
  ~RouterCollector();
  RouterCollector(const RouterCollector&) = delete;
  RouterCollector& operator=(const RouterCollector&) = delete;
  Status Start(const std::string& directory, const std::string& workload,
               const std::string& model_index, RouterTraceConfig config,
               uint32_t max_length, uint64_t max_bytes);
  // Single admitted sequence owns these calls. Scheduler handoff is ordered
  // by the existing request CV/mutex; Begin/End run before releasing its slot.
  void BeginRequest(std::span<const int32_t> tokens,
                    const std::string& http_id);
  void EndRequest(RequestOutcome outcome, uint64_t output_tokens);
  void BeginForward(RouteStage stage, uint64_t position, uint32_t rows);
  Status CaptureLayer(uint32_t layer, const int32_t* ids, int rows, int top_k,
                      cudaStream_t stream);
  cudaError_t Readback(cudaStream_t stream);
  void Complete(bool submitted, bool gpu_complete, bool committed);
  bool HasCudaFailure() const { return failure_.load() == Failure::kCuda; }
  void Stop();  // Called only after request/scheduler users have stopped.

 private:
  enum class Kind { kBegin, kForward, kEnd };
  enum class Failure {
    kNone,
    kQueueFull,
    kQuota,
    kIO,
    kContract,
    kCuda,
    kRequestLimit,
    kAllocation
  };
  struct Slot {
    std::atomic<bool> ready{false};
    Kind kind = Kind::kBegin;
    int32_t* host = nullptr;
    uint64_t request = 0;
    uint64_t forward = 0;
    uint64_t position = 0;
    uint64_t output_tokens = 0;
    uint32_t rows = 0;
    uint32_t layers = 0;
    RouteStage stage = RouteStage::kPrefill;
    RequestOutcome outcome = RequestOutcome::kFailed;
    bool submitted = false, complete = false, committed = false;
    std::array<char, 129> http_id{};
  };
  Slot* Reserve(Kind kind);
  void Publish();
  void Fail(Failure reason);
  Status AllocationFailure(cudaError_t error);
  void Worker();
  void Manifest(bool complete);
  static const char* Reason(Failure reason);
  static constexpr size_t kSlots = 4;
  std::array<Slot, kSlots> slots_;
  size_t producer_ = 0;
  Slot* active_ = nullptr;
  cudaError_t active_error_ = cudaSuccess;
  int32_t* device_ = nullptr;
  RouterTraceConfig config_;
  Digest model_config_digest_{}, command_digest_{}, environment_digest_{};
  uint32_t max_length_ = 0;
  size_t slot_bytes_ = 0;
  uint64_t max_bytes_ = 0, written_ = 0, forward_id_ = 0;
  std::atomic<uint64_t> request_id_{0};
  uint64_t finished_ = 0;
  bool request_open_ = false;
  std::string directory_;
  std::atomic<Failure> failure_{Failure::kNone};
  std::atomic<bool> stopping_{false};
  std::thread worker_;
};
}  // namespace q4t::trace
