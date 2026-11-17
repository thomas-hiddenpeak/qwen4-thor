// Native ASR execution. The caller owns verified weights, workspaces and the
// shared GPU execution lock. Methods synchronize before advancing progress.
#pragma once

#include <array>
#include <span>

#include "q4t/audio/asr_layout.h"
#include "q4t/model/embedding_model.h"

namespace q4t::audio {

struct AsrAffineWeights {
  const uint16_t* weight = nullptr;
  const uint16_t* bias = nullptr;
};

struct AsrEncoderLayerWeights {
  AsrAffineWeights input_norm;
  AsrAffineWeights post_norm;
  AsrAffineWeights q, k, v, o;
  AsrAffineWeights fc1, fc2;
};

struct AsrDeviceWeights {
  std::array<AsrAffineWeights, 3> convolution;
  const uint16_t* convolution_out = nullptr;
  AsrAffineWeights final_norm, proj1, proj2;
  std::array<AsrEncoderLayerWeights, AsrConfig::kEncoderLayers> encoder;
  model::EmbeddingDeviceWeights decoder;
  const uint16_t* lm_head = nullptr;
};

using AsrComputeResult = model::EmbeddingComputeResult;

// No allocation, I/O or CUDA calls; validates the layout before binding.
Status BindAsrDeviceWeights(const void* data, size_t bytes,
                            const AsrWeightLayout& layout,
                            AsrDeviceWeights* out);

class AsrEncoderRun {
 public:
  AsrComputeResult Begin(const AsrDeviceWeights& weights,
                         std::span<const float> mel, int frames,
                         void* workspace, size_t bytes,
                         cudaStream_t stream = nullptr);
  AsrComputeResult NextChunk(cudaStream_t stream = nullptr);
  AsrComputeResult NextLayer(cudaStream_t stream = nullptr);
  AsrComputeResult Finish(cudaStream_t stream = nullptr);
  int completed_chunks() const { return completed_chunks_; }
  int completed_layers() const { return completed_layers_; }
  // Device view is available only after successful Finish; no hidden copy.
  std::span<const uint16_t> output() const;

 private:
  const AsrDeviceWeights* weights_ = nullptr;
  uint8_t* workspace_ = nullptr;
  AsrEncoderLayout layout_;
  int completed_chunks_ = 0;
  int completed_layers_ = 0;
  bool started_ = false;
  bool failed_ = false;
  bool finished_ = false;
};

// One real autoregressive step, recomputing the full causal prefix. Persistent
// KV storage is a separate scheduling optimization; no previous token or audio
// position is omitted here. The caller controls context/output/time budgets.
class AsrDecoderStep {
 public:
  AsrComputeResult Begin(const AsrDeviceWeights& weights,
                         std::span<const int32_t> tokens, size_t audio_start,
                         std::span<const uint16_t> device_audio,
                         void* workspace, size_t bytes,
                         cudaStream_t stream = nullptr);
  AsrComputeResult NextLayer(cudaStream_t stream = nullptr);
  // Official LM head has BF16 output; caller converts to FP32 for sampling.
  AsrComputeResult Finish(std::span<uint16_t> logits,
                          cudaStream_t stream = nullptr);
  int completed_layers() const { return dense_.completed_layers(); }

 private:
  const AsrDeviceWeights* weights_ = nullptr;
  uint8_t* workspace_ = nullptr;
  AsrDecoderLayout layout_;
  model::EmbeddingRun dense_;
  bool started_ = false;
  bool failed_ = false;
  bool finished_ = false;
};

}  // namespace q4t::audio
