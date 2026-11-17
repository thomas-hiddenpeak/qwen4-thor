// Native fixed FP32 ECAPA execution; caller owns weights and bounded workspace.
#pragma once

#include <cuda_runtime_api.h>

#include <span>

#include "q4t/audio/speaker_layout.h"

namespace q4t::audio {
struct SpeakerComputeResult {
  Status status;
  cudaError_t device_error = cudaSuccess;
  bool ok() const { return status.ok() && device_error == cudaSuccess; }
};
class SpeakerRun {
 public:
  // Input is the normalized CPU [80, frames] matrix. The caller owns the
  // shared device mutex and keeps weights/workspace alive until all stages end.
  SpeakerComputeResult Begin(const SpeakerDeviceWeights& weights,
                             std::span<const float> mel, int frames,
                             void* workspace, size_t bytes,
                             cudaStream_t stream = nullptr);
  // Initial TDNN, three residual blocks, aggregation, pooling, final head.
  // A successful stage synchronizes before publishing actual progress.
  SpeakerComputeResult NextStage(cudaStream_t stream = nullptr);
  SpeakerComputeResult Finish(std::span<float> embedding,
                              cudaStream_t stream = nullptr);
  int completed_stages() const { return completed_; }

 private:
  const SpeakerDeviceWeights* weights_ = nullptr;
  uint8_t* workspace_ = nullptr;
  SpeakerWorkspaceLayout layout_;
  int completed_ = 0;
  bool started_ = false, failed_ = false, finished_ = false;
};
}  // namespace q4t::audio
