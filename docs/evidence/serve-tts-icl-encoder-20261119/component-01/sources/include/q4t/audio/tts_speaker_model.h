// Request-owned native Base speaker execution. No implicit model restarts.
#pragma once

#include "q4t/audio/tts_model.h"
#include "q4t/audio/tts_speaker_layout.h"

namespace q4t::audio {
class TtsSpeakerRun {
 public:
  static constexpr int kStages = 47;

  // Host FP32 features [128, frames]. Weights and workspace are caller-owned
  // device allocations. Every call drains its stream before returning.
  TtsComputeResult Begin(const TtsSpeakerDeviceWeights& weights,
                         std::span<const float> mel, size_t frames,
                         void* workspace, size_t bytes,
                         cudaStream_t stream = nullptr);
  // At most one convolution/reduction stage. The caller checks request
  // cancellation/deadline between calls; failed instances cannot be reused.
  TtsComputeResult NextStage(cudaStream_t stream = nullptr);
  // Copy the complete 2048-element host result atomically; no partial vector.
  TtsComputeResult Finish(std::span<float> embedding,
                          cudaStream_t stream = nullptr);
  int completed_stages() const { return completed_; }

 private:
  const TtsSpeakerDeviceWeights* weights_ = nullptr;
  uint8_t* workspace_ = nullptr;
  TtsSpeakerWorkspace layout_;
  int completed_ = 0;
  bool started_ = false, failed_ = false, finished_ = false;
};
}  // namespace q4t::audio
