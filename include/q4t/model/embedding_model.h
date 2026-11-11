// Native dense Qwen3 embedding execution; no filesystem or allocation here.
#pragma once

#include <cuda_runtime_api.h>

#include <array>
#include <cstdint>
#include <span>

#include "q4t/model/embedding_config.h"
#include "q4t/model/embedding_layout.h"

namespace q4t::model {

#ifdef Q4T_EMBEDDING_TRACE
// Replay-only instrumentation. Production compiles no trace calls or state.
void TraceEmbeddingOperand(int layer, const char* name,
                            const uint16_t* data, size_t values);
#endif

struct EmbeddingLayerWeights {
  const uint16_t* input_norm = nullptr;
  const uint16_t* post_norm = nullptr;
  const uint16_t* q_norm = nullptr;
  const uint16_t* k_norm = nullptr;
  const uint16_t* q = nullptr;
  const uint16_t* k = nullptr;
  const uint16_t* v = nullptr;
  const uint16_t* o = nullptr;
  const uint16_t* gate = nullptr;
  const uint16_t* up = nullptr;
  const uint16_t* down = nullptr;
};

struct EmbeddingDeviceWeights {
  const uint16_t* tokens = nullptr;
  const uint16_t* final_norm = nullptr;
  std::array<EmbeddingLayerWeights, EmbeddingConfig::kLayers> layers;
};

// Caller owns the verified, fully copied contiguous device payload. No I/O
// or CUDA calls. The binding retains neither host mappings nor file handles.
Status BindEmbeddingDeviceWeights(const void* data, size_t bytes,
                                   const EmbeddingWeightLayout& layout,
                                   EmbeddingDeviceWeights* out);

struct EmbeddingComputeResult {
  Status status;
  // CUDA failures remain distinguishable from malformed input or BLAS plan
  // failure. A genuine device error must propagate to shared GPU protection.
  cudaError_t device_error = cudaSuccess;
  bool ok() const { return status.ok() && device_error == cudaSuccess; }
};

// Request-owned workspace; caller holds the global GPU execution mutex for
// each method, can yield between completed layers, and applies admission,
// cancellation and memory-failure policy. Successful methods synchronize the
// stream and only then advance state. No generation or KV-cache allocation.
class EmbeddingRun {
 public:
  EmbeddingComputeResult Begin(const EmbeddingDeviceWeights& weights,
                               std::span<const int32_t> tokens,
                               void* workspace, size_t bytes,
                               cudaStream_t stream = nullptr);
  EmbeddingComputeResult NextLayer(cudaStream_t stream = nullptr);
  // LAST after final RMSNorm, BF16 output. Existing endpoint serialization
  // applies selected dimensions and L2 in FP32, shared with other backends.
  EmbeddingComputeResult Finish(std::span<uint16_t> last_hidden,
                                cudaStream_t stream = nullptr);
  int completed_layers() const { return completed_layers_; }
  bool started() const { return started_; }
  bool finished() const { return finished_; }

 private:
  const EmbeddingDeviceWeights* weights_ = nullptr;
  uint8_t* workspace_ = nullptr;
  EmbeddingWorkspaceLayout layout_;
  int completed_layers_ = 0;
  bool started_ = false;
  bool finished_ = false;
  bool failed_ = false;
};

}  // namespace q4t::model
