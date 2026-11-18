// Native F32 SepFormer. The caller owns weights, workspace and device locking.
#pragma once

#include <cuda_runtime_api.h>

#include <span>

#include "q4t/audio/separator_layout.h"

namespace q4t::audio {
struct SeparatorComputeResult {
  Status status;
  cudaError_t device_error = cudaSuccess;
  bool ok() const { return status.ok() && device_error == cudaSuccess; }
};
class SeparatorRun {
 public:
  static constexpr int kStages = 36;
  // Exact 8 kHz mono input. Begin/NextStage synchronize before returning;
  // between stages the owner may release its shared GPU lock or cancel.
  SeparatorComputeResult Begin(const SeparatorDeviceWeights& weights,
                               std::span<const float> pcm, void* workspace,
                               size_t bytes, cudaStream_t stream = nullptr);
  SeparatorComputeResult NextStage(cudaStream_t stream = nullptr);
  // Source-major [2, input_samples]. No peak normalization. The incomplete
  // encoder tail is zero padded as in upstream separate_batch.
  SeparatorComputeResult Finish(std::span<float> sources,
                                cudaStream_t stream = nullptr);
  int completed_stages() const { return completed_; }

 private:
  const SeparatorDeviceWeights* weights_ = nullptr;
  uint8_t* workspace_ = nullptr;
  SeparatorWorkspaceLayout layout_;
  int completed_ = 0;
  bool started_ = false, failed_ = false, finished_ = false;
};
}  // namespace q4t::audio
