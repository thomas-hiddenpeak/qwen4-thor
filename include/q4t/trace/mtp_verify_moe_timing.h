#pragma once

#include <cuda_runtime_api.h>

#include <array>
#include <cstddef>
#include <cstdint>
#include <memory>
#include <string>
#include <string_view>

namespace q4t::trace {

// Initial collection is request-owned and restricted to B1/T4 main verify.
// Default-null scopes do not read clocks or call CUDA. Event operations never
// synchronize or introduce dependencies; elapsed values include host gaps.
enum class VerifyMoeStepPoint {
  kEngineBegin,
  kDraftEnd,
  kVerifyReadbackEnd,
  kAcceptEnd,
  kExtendReadbackEnd,
  kEngineEnd,
};
enum class VerifyMoeHostPoint {
  kMoeBegin,
  kRoutedBegin,
  kCountsWaitBegin,
  kCountsWaitEnd,
  kOffsetsEnd,
  kExpertLoopBegin,
  kExpertLoopEnd,
  kRoutedEnd,
  kMoeEnd,
};
enum class VerifyMoeGpuPoint {
  kMoeBegin,
  kRouterEnd,
  kTopkZeroEnd,
  kListEnd,
  kCountsCopyEnd,
  kExpertsJoined,
  kRoutedEnd,
  kSharedGuEnd,
  kSharedActEnd,
  kSharedDnEnd,
  kMoeEnd,
};
enum class VerifyMoeExpertPoint {
  kGatherBegin,
  kGatherEnd,
  kGuEnd,
  kActivationEnd,
  kDnEnd,
};

// The sole injection seam for finite host contracts. Production uses CUDA;
// test implementations return statuses without creating a device/context.
class MtpVerifyMoeEvents {
 public:
  virtual ~MtpVerifyMoeEvents() = default;
  virtual cudaError_t Create(void** handle) noexcept = 0;
  virtual cudaError_t Record(void* handle, cudaStream_t stream) noexcept = 0;
  virtual cudaError_t Query(void* handle) noexcept = 0;
  virtual cudaError_t Elapsed(float* ms, void* begin, void* end) noexcept = 0;
  virtual cudaError_t Destroy(void* handle) noexcept = 0;
};

class MtpVerifyMoeTiming;
class MtpVerifyMoeStep;

class MtpVerifyMoeCall {
 public:
  void MarkGpu(VerifyMoeGpuPoint point, cudaStream_t stream) noexcept;
  void MarkHost(VerifyMoeHostPoint point) noexcept;
  void SetCounts(const int32_t* counts, int experts, int rows, int k) noexcept;
  void SetStreams(int streams) noexcept;
  // ordinal is the existing ascending-expert traversal's zero-based active
  // ordinal. Unselected ordinals perform no event operations or clock reads.
  void MarkExpert(int ordinal, VerifyMoeExpertPoint point,
                  cudaStream_t stream) noexcept;

 private:
  friend class MtpVerifyMoeStep;
  friend class MtpVerifyMoeTiming;
  MtpVerifyMoeTiming* owner_ = nullptr;
  int step_ = -1;
  int layer_ = -1;
  int sample_ = -1;
  bool seen_ = false;
  bool counts_set_ = false;
  bool streams_set_ = false;
  std::array<int, 5> histogram_{};
  int active_ = 0;
  int routes_ = 0;
  int invalid_counts_ = 0;
  int streams_ = 0;
};

class MtpVerifyMoeStep {
 public:
  void Mark(VerifyMoeStepPoint point) noexcept;
  void Invalidate(const char* reason) noexcept;
  void SetEngineResult(int accepted_drafts, int returned, int verify_rows,
                       int extend_rows) noexcept;
  void SetDelivery(int generated) noexcept;
  MtpVerifyMoeCall* BeginLayer(int layer, int rows) noexcept;

 private:
  friend class MtpVerifyMoeTiming;
  MtpVerifyMoeTiming* owner_ = nullptr;
  int index_ = -1;
  int position_ = -1;
  size_t marks_ = 0;
  size_t layers_ = 0;
  std::array<int64_t, 6> host_ns_{};
  std::array<MtpVerifyMoeCall, 48> calls_{};
  int accepted_ = -1;
  int returned_ = -1;
  int verify_rows_ = -1;
  int extend_rows_ = -1;
  int delivered_ = -1;
};

MtpVerifyMoeStep* ActiveMtpVerifyMoeStep() noexcept;
class MtpVerifyMoeScope {
 public:
  explicit MtpVerifyMoeScope(MtpVerifyMoeStep* step) noexcept;
  ~MtpVerifyMoeScope() noexcept;
  MtpVerifyMoeScope(const MtpVerifyMoeScope&) = delete;
  MtpVerifyMoeScope& operator=(const MtpVerifyMoeScope&) = delete;

 private:
  MtpVerifyMoeStep* previous_;
};

class MtpVerifyMoeTiming {
 public:
  MtpVerifyMoeTiming(std::string response_id, int prompt_tokens, int max_tokens,
                     int k, bool supported = true,
                     MtpVerifyMoeEvents* events = nullptr);
  ~MtpVerifyMoeTiming() noexcept;
  MtpVerifyMoeTiming(const MtpVerifyMoeTiming&) = delete;
  MtpVerifyMoeTiming& operator=(const MtpVerifyMoeTiming&) = delete;
  MtpVerifyMoeStep* BeginStep(int position, int k) noexcept;
  void Invalidate(const char* reason) noexcept;
  void Finish(int generated, int mtp_steps, bool plain_tail,
              std::string_view finish_reason, std::string_view fallback,
              bool success) noexcept;

 private:
  friend class MtpVerifyMoeStep;
  friend class MtpVerifyMoeCall;
  struct Impl;
  int64_t NowNs() const noexcept;
  void Report();
  std::unique_ptr<Impl> impl_;
};

}  // namespace q4t::trace
