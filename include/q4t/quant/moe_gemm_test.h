// Bounded observation interface for the T4 batch-gather numerical contract.
// Production MoERoutedForward never allocates or populates these buffers.
#pragma once

#include <array>
#include <cstddef>
#include <cstdint>

#include "q4t/quant/lt_cache.h"
#include "q4t/quant/moe_gemm.h"

namespace q4t {
namespace quant {

constexpr int kMoEBatchExperts = 512;
constexpr int kMoEBatchSlots = 40;
constexpr int kMoEBatchStreams = 4;

struct MoEBatchGatherRow {
  int expert;
  int local_row;
  int active_ordinal;
};
struct MoEBatchGatherRows {
  MoEBatchGatherRow rows[kMoEBatchSlots];
  int count;
};
struct MoEBatchGatherPlan {
  MoEBatchGatherRows streams[kMoEBatchStreams];
  int active_experts;
};

bool MoEBatchGatherShapeSupported(int M, int k, int E, int hs, int moe_is,
                                 int streams);
// Validates the original counts before clamping. No CUDA work is submitted.
bool MakeMoEBatchGatherPlan(const int32_t* counts, int E,
                            MoEBatchGatherPlan* plan);

struct MoECapturedGemm {
  LtPlanKey key;
  uint32_t alpha_bits = 0;
  std::array<uint8_t, sizeof(cublasLtMatmulAlgo_t)> algo{};
  bool has_algo = false;
};
struct MoECapturedExpert {
  int expert = -1;
  int rows = 0;
  int active_ordinal = -1;
  int stream_index = -1;
  int grouped_offset = 0;
  MoECapturedGemm gu;
  MoECapturedGemm dn;
};

// Every region has a 256-byte prefix/suffix guard. The caller initializes
// the whole device buffer to kSentinel. SF atoms contain undefined padding;
// only logical SfOffset(row, group) bytes are numerical observations.
struct MoERoutedCaptureLayout {
  static constexpr uint8_t kSentinel = 0xa5;
  static constexpr size_t kGuard = 256;
  static constexpr std::array<size_t, 6> kStageBytes = {
      5120, 20480, 10240, 1280, 5120, 20480};
  static constexpr size_t kExpertStride = 65792;
  static constexpr size_t kExpertsBytes = kExpertStride * kMoEBatchSlots;
  static constexpr size_t StageOffset(int ordinal, int stage) {
    size_t offset = static_cast<size_t>(ordinal) * kExpertStride + kGuard;
    for (int i = 0; i < stage; ++i)
      offset += kStageBytes[i] + 2 * kGuard;
    return offset;
  }
  static constexpr size_t kTokenListBytes = 512 * 4 * sizeof(int32_t);
  static constexpr size_t kTokenListOffset = kExpertsBytes + kGuard;
  static constexpr size_t kRowMapBytes = 40 * sizeof(int32_t);
  static constexpr size_t kRowMapOffset =
      kTokenListOffset + kTokenListBytes + 2 * kGuard;
  static constexpr size_t kArenaBytes =
      MoEBatchGatherExtraBytes() + 2 * kGuard;
  static constexpr size_t kArenaOffset =
      kRowMapOffset + kRowMapBytes + 2 * kGuard;
  static constexpr size_t kBytes = kArenaOffset + kArenaBytes + kGuard;
};
static_assert(MoERoutedCaptureLayout::StageOffset(0, 5) +
                  MoERoutedCaptureLayout::kStageBytes[5] +
                  MoERoutedCaptureLayout::kGuard ==
              MoERoutedCaptureLayout::kExpertStride);

struct MoERoutedCapture {
  uint8_t* device_buffer = nullptr;
  size_t device_bytes = 0;
  bool inject_after_first_extra_batch = false;
  bool injection_triggered = false;
  bool cleanup_joined = false;
  bool cleanup_freed = false;
  bool batch_applied = false;
  int active_experts = 0;
  int streams = 0;
  std::array<int32_t, 512> counts{};
  std::array<int32_t, 513> offsets{};
  std::array<MoECapturedExpert, 40> experts{};
};

// The caller owns the capture device buffer and performs one readback after
// this call and its caller stream complete. All captures are D2D copies on
// the original compute stream, before scratch is overwritten. This entry
// accepts only the bounded T4 shape and four streams.
Status MoERoutedForwardForTest(
    const uint16_t* x, const int32_t* expert_ids, const float* router_w,
    float* y, const MoEWeightLayout& weights, void* workspace, void* gemm_ws,
    size_t gemm_ws_bytes, int M, int k, cudaStream_t stream,
    bool batch_requested, MoERoutedCapture* capture);

}  // namespace quant
}  // namespace q4t
