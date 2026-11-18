// Native TTS computation. The caller owns all weights/workspace and holds
// the shared GPU execution lock. Each call drains its stream before return.
#pragma once

#include <cuda_runtime_api.h>

#include "q4t/audio/tts_generation.h"
#include "q4t/audio/tts_layout.h"

namespace q4t::audio {

#ifdef Q4T_TTS_TRACE
// Qualification-only capture; absent from production builds.
void TraceTtsOperand(const char* name, size_t frame, size_t group,
                     std::span<const float> device_values);
#endif

struct TtsComputeResult {
  Status status;
  cudaError_t device_error = cudaSuccess;
  bool ok() const { return status.ok() && device_error == cudaSuccess; }
};

// Request-owned incremental dense Qwen3 execution. Cache positions commit
// only after every layer and final norm succeed. A failed run is not reused.
class TtsDenseRun {
 public:
  TtsComputeResult Init(const TtsDenseWeights& weights, TtsVariant variant,
                        bool predictor, size_t max_rows, size_t context,
                        void* workspace, size_t bytes,
                        cudaStream_t stream = nullptr);
  TtsComputeResult Reset(cudaStream_t stream = nullptr);
  TtsComputeResult Begin(std::span<const float> input,
                         cudaStream_t stream = nullptr);
  TtsComputeResult NextLayer(cudaStream_t stream = nullptr);
  TtsComputeResult Finish(std::span<const float>* last_hidden,
                          cudaStream_t stream = nullptr);
  int completed_layers() const { return layer_; }
  int cached_tokens() const { return past_; }

 private:
  const TtsDenseWeights* weights_ = nullptr;
  uint8_t* workspace_ = nullptr;
  TtsDenseLayout layout_;
  int past_ = 0, rows_ = 0, layer_ = 0;
  bool initialized_ = false, active_ = false, failed_ = false;
};

// Begin prepares actual projected text/codec embeddings. Advance performs
// at most one decoder layer, or a head/sample/conditioning transition. The
// caller checks cancellation/time budgets between calls; no worker owns an
// uninterruptible full utterance loop. Only complete frames become visible.
class TtsGenerationRun {
 public:
  TtsComputeResult Begin(const TtsDeviceWeights& weights,
                         const TtsPrompt& prompt, size_t max_frames,
                         const TtsSampling& talker_sampling,
                         const TtsSampling& predictor_sampling, uint64_t seed,
                         void* workspace, size_t bytes,
                         cudaStream_t stream = nullptr);
  TtsComputeResult Advance(cudaStream_t stream = nullptr);
  bool done() const { return state_.ended() || state_.at_limit(); }
  bool ended() const { return state_.ended(); }
  bool at_limit() const { return state_.at_limit(); }
  size_t frames() const { return state_.frames(); }
  std::span<const TtsCodes> codes() const { return state_.completed(); }

 private:
  TtsComputeResult Head(TtsDenseRun& run, const float* head,
                        std::span<float> logits, bool save_hidden,
                        cudaStream_t stream);
  TtsComputeResult BeginPredictor(int32_t code, cudaStream_t stream);
  TtsComputeResult ContinuePredictor(size_t group, int32_t code,
                                     cudaStream_t stream);
  TtsComputeResult ContinueTalker(cudaStream_t stream);
  TtsComputeResult ProjectPredictor(int rows, cudaStream_t stream);

  const TtsDeviceWeights* weights_ = nullptr;
  uint8_t* workspace_ = nullptr;
  TtsGenerationLayout layout_;
  TtsPrompt prompt_;
  TtsDenseRun talker_, predictor_;
  TtsGenerationState state_;
  TtsCodeSampler talker_sampler_, predictor_sampler_;
  bool initialized_ = false, predicting_ = false, failed_ = false;
};

}  // namespace q4t::audio
