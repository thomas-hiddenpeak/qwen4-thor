#include "q4t/model/embedding_model.h"

#include <cuda_bf16.h>
#include <math_constants.h>

#include <algorithm>
#include <string>

#include "q4t/model/linear.h"

namespace q4t::model {
namespace {
using C = EmbeddingConfig;
constexpr int kThreads = 256;

__device__ float ReadBf16(const uint16_t* p, size_t i) {
  return __bfloat162float(__ushort_as_bfloat16(p[i]));
}
__device__ uint16_t WriteBf16(float x) {
  return __bfloat16_as_ushort(__float2bfloat16_rn(x));
}
__device__ float RoundBf16(float x) {
  return __bfloat162float(__float2bfloat16_rn(x));
}

template <bool kMaximum>
__device__ float Reduce(float value, float* scratch) {
  scratch[threadIdx.x] = value;
  __syncthreads();
  for (int n = blockDim.x / 2; n; n /= 2) {
    if (threadIdx.x < n)
      scratch[threadIdx.x] =
          kMaximum ? fmaxf(scratch[threadIdx.x], scratch[threadIdx.x + n])
                   : scratch[threadIdx.x] + scratch[threadIdx.x + n];
    __syncthreads();
  }
  return scratch[0];
}

__global__ void Lookup(const uint16_t* weights, const int32_t* ids,
                       uint16_t* hidden) {
  const int t = blockIdx.x;
  for (int d = threadIdx.x; d < C::kHidden; d += blockDim.x)
    hidden[t * C::kHidden + d] =
        weights[static_cast<size_t>(ids[t]) * C::kHidden + d];
}

__global__ void RopeTable(uint16_t* rope, int count) {
  const int index = blockIdx.x * blockDim.x + threadIdx.x;
  if (index >= count * (C::kHeadDim / 2)) return;
  const int t = index / (C::kHeadDim / 2);
  const int d = index % (C::kHeadDim / 2);
  const float inverse = 1.0f / powf(C::kRopeTheta, 2.0f * d / C::kHeadDim);
  const float angle = __fmul_rn(static_cast<float>(t), inverse);
  rope[t * C::kHeadDim + d] = WriteBf16(cosf(angle));
  rope[t * C::kHeadDim + d + C::kHeadDim / 2] = WriteBf16(sinf(angle));
}

__global__ void Norm(const uint16_t* input, const uint16_t* weight,
                     uint16_t* output) {
  __shared__ float reduction[kThreads];
  const size_t base = static_cast<size_t>(blockIdx.x) * C::kHidden;
  float sum = 0;
  for (int d = threadIdx.x; d < C::kHidden; d += blockDim.x) {
    const float x = ReadBf16(input, base + d);
    sum += __fmul_rn(x, x);
  }
  const float rms = rsqrtf(Reduce<false>(sum, reduction) / C::kHidden +
                           C::kNormEpsilon);
  for (int d = threadIdx.x; d < C::kHidden; d += blockDim.x) {
    // Match the trained ordinary RMSNorm: round normalization to BF16
    // before multiplying by its BF16 learned weight (not centered norm).
    const float normalized = RoundBf16(__fmul_rn(ReadBf16(input, base + d), rms));
    output[base + d] = WriteBf16(__fmul_rn(normalized, ReadBf16(weight, d)));
  }
}

__global__ void NormRope(const uint16_t* input, const uint16_t* weight,
                         const uint16_t* rope, uint16_t* heads, int tokens,
                         int padded, int count) {
  __shared__ float reduction[C::kHeadDim];
  __shared__ float normalized[C::kHeadDim];
  const int t = blockIdx.x, h = blockIdx.y, d = threadIdx.x;
  const size_t destination = (static_cast<size_t>(h) * padded + t) *
                             C::kHeadDim + d;
  if (t >= tokens) {
    heads[destination] = 0;
    return;
  }
  const float x = ReadBf16(input, (t * count + h) * C::kHeadDim + d);
  const float rms = rsqrtf(Reduce<false>(__fmul_rn(x, x), reduction) /
                               C::kHeadDim + C::kNormEpsilon);
  normalized[d] = RoundBf16(__fmul_rn(RoundBf16(__fmul_rn(x, rms)),
                                     ReadBf16(weight, d)));
  __syncthreads();
  const int half = C::kHeadDim / 2, pair = (d + half) % C::kHeadDim;
  const float rotated = d < half ? -normalized[pair] : normalized[pair];
  const float cosine = ReadBf16(rope, t * C::kHeadDim + d % half);
  const float sine = ReadBf16(rope, t * C::kHeadDim + half + d % half);
  // Each tensor multiply in eager BF16 RoPE rounds before the addition.
  heads[destination] = WriteBf16(__fadd_rn(
      RoundBf16(__fmul_rn(normalized[d], cosine)),
      RoundBf16(__fmul_rn(rotated, sine))));
}

__global__ void TransposeValues(const uint16_t* values, uint16_t* transposed,
                                int tokens, int padded) {
  const int index = blockIdx.x * blockDim.x + threadIdx.x;
  if (index >= padded * C::kKvHeads * C::kHeadDim) return;
  const int t = index % padded;
  const int d = index / padded % C::kHeadDim;
  const int h = index / padded / C::kHeadDim;
  transposed[index] = t < tokens
      ? values[(t * C::kKvHeads + h) * C::kHeadDim + d] : 0;
}

__global__ void CausalSoftmax(uint16_t* scores, int padded) {
  __shared__ float reduction[kThreads];
  const int row = blockIdx.x;
  const size_t base = static_cast<size_t>(row) * padded;
  constexpr float scale = 0.08838834764831845f;  // 1/sqrt(128).
  float maximum = -CUDART_INF_F;
  for (int j = threadIdx.x; j < padded; j += blockDim.x) {
    // QK matmul BF16 -> BF16 scaling -> FP32 softmax -> BF16 PV operand.
    const float score = j <= row
        ? RoundBf16(__fmul_rn(ReadBf16(scores, base + j), scale))
        : -CUDART_INF_F;
    scores[base + j] = WriteBf16(score);
    maximum = fmaxf(maximum, score);
  }
  maximum = Reduce<true>(maximum, reduction);
  // Reduce's shared storage is reused only after every thread consumed it.
  __syncthreads();
  float sum = 0;
  for (int j = threadIdx.x; j <= row; j += blockDim.x)
    sum += expf(ReadBf16(scores, base + j) - maximum);
  sum = Reduce<false>(sum, reduction);
  for (int j = threadIdx.x; j < padded; j += blockDim.x)
    scores[base + j] = WriteBf16(j <= row
        ? expf(ReadBf16(scores, base + j) - maximum) / sum : 0.0f);
}

__global__ void MergeHead(const uint16_t* head, uint16_t* output, int tokens,
                          int h) {
  const int index = blockIdx.x * blockDim.x + threadIdx.x;
  if (index >= tokens * C::kHeadDim) return;
  output[(index / C::kHeadDim * C::kQueryHeads + h) * C::kHeadDim +
         index % C::kHeadDim] = head[index];
}

__global__ void Residual(uint16_t* hidden, const uint16_t* projected, int n) {
  const int i = blockIdx.x * blockDim.x + threadIdx.x;
  if (i < n)
    hidden[i] = WriteBf16(__fadd_rn(ReadBf16(hidden, i), ReadBf16(projected, i)));
}

__global__ void SwiGlu(uint16_t* gate, const uint16_t* up, int n) {
  const int i = blockIdx.x * blockDim.x + threadIdx.x;
  if (i >= n) return;
  const float x = ReadBf16(gate, i);
  const float activated = RoundBf16(x / (1.0f + expf(-x)));
  gate[i] = WriteBf16(__fmul_rn(activated, ReadBf16(up, i)));
}

EmbeddingComputeResult Invalid(const char* reason) {
  return {Status::Fail(reason), cudaSuccess};
}

// Drain all submitted work even if a host-side plan/launch failed. The caller
// may release/reuse buffers after this boundary, subject to device protection.
EmbeddingComputeResult Complete(Status status, cudaStream_t stream) {
  const auto launch = cudaPeekAtLastError();
  const auto completion = cudaStreamSynchronize(stream);
  const auto error = completion != cudaSuccess ? completion : launch;
  if (error != cudaSuccess)
    return {Status::Fail(std::string("embedding CUDA failure: ") +
                         cudaGetErrorString(error)), error};
  return {std::move(status), cudaSuccess};
}
}  // namespace

Status BindEmbeddingDeviceWeights(const void* data, size_t bytes,
                                   const EmbeddingWeightLayout& layout,
                                   EmbeddingDeviceWeights* out) {
  if (!data || !out || bytes != layout.bytes ||
      layout.bytes != EmbeddingWeightBytes())
    return Status::Fail("embedding device weight buffer differs");
  const auto contracts = EmbeddingTensorContracts();
  if (contracts.size() != layout.tensors.size())
    return Status::Fail("embedding weight layout differs");
  for (size_t i = 0; i < contracts.size(); ++i) {
    const auto& actual = layout.tensors[i];
    if (actual.name != contracts[i].name || actual.bytes != contracts[i].bytes ||
        actual.offset > bytes || actual.bytes > bytes - actual.offset ||
        actual.offset % 2)
      return Status::Fail("embedding device tensor extent differs");
  }
  const auto pointer = [&](const std::string& name) {
    const auto found = std::find_if(layout.tensors.begin(), layout.tensors.end(),
        [&](const auto& tensor) { return tensor.name == name; });
    return reinterpret_cast<const uint16_t*>(
        static_cast<const uint8_t*>(data) + found->offset);
  };
  EmbeddingDeviceWeights weights;
  weights.tokens = pointer("embed_tokens.weight");
  weights.final_norm = pointer("norm.weight");
  for (int i = 0; i < C::kLayers; ++i) {
    auto& w = weights.layers[i];
    const auto p = "layers." + std::to_string(i) + ".";
    w.input_norm = pointer(p + "input_layernorm.weight");
    w.post_norm = pointer(p + "post_attention_layernorm.weight");
    w.q_norm = pointer(p + "self_attn.q_norm.weight");
    w.k_norm = pointer(p + "self_attn.k_norm.weight");
    w.q = pointer(p + "self_attn.q_proj.weight");
    w.k = pointer(p + "self_attn.k_proj.weight");
    w.v = pointer(p + "self_attn.v_proj.weight");
    w.o = pointer(p + "self_attn.o_proj.weight");
    w.gate = pointer(p + "mlp.gate_proj.weight");
    w.up = pointer(p + "mlp.up_proj.weight");
    w.down = pointer(p + "mlp.down_proj.weight");
  }
  *out = weights;
  return {};
}

EmbeddingComputeResult EmbeddingRun::Begin(
    const EmbeddingDeviceWeights& weights, std::span<const int32_t> tokens,
    void* workspace, size_t bytes, cudaStream_t stream) {
  if (started_ || failed_ || tokens.empty() || tokens.size() > C::kInputTokens ||
      !workspace || reinterpret_cast<uintptr_t>(workspace) % 256 ||
      !weights.tokens || !weights.final_norm)
    return Invalid("invalid embedding execution begin");
  for (const auto& w : weights.layers)
    if (!w.input_norm || !w.post_norm || !w.q_norm || !w.k_norm || !w.q ||
        !w.k || !w.v || !w.o || !w.gate || !w.up || !w.down)
      return Invalid("incomplete embedding layer weights");
  for (const auto token : tokens)
    if (token < 0 || token >= C::kVocab)
      return Invalid("embedding token ID outside model vocabulary");
  auto status = PlanEmbeddingWorkspace(static_cast<int>(tokens.size()), &layout_);
  if (!status || bytes < layout_.bytes)
    return Invalid("embedding workspace too small");
  weights_ = &weights;
  workspace_ = static_cast<uint8_t*>(workspace);
  auto* ids = reinterpret_cast<int32_t*>(workspace_ + layout_.ids);
  const auto copied = cudaMemcpyAsync(ids, tokens.data(), tokens.size_bytes(),
                                      cudaMemcpyHostToDevice, stream);
  if (copied == cudaSuccess) {
    Lookup<<<layout_.tokens, kThreads, 0, stream>>>(weights.tokens, ids,
        reinterpret_cast<uint16_t*>(workspace_ + layout_.hidden));
    const int count = layout_.tokens * (C::kHeadDim / 2);
    RopeTable<<<(count + kThreads - 1) / kThreads, kThreads, 0, stream>>>(
        reinterpret_cast<uint16_t*>(workspace_ + layout_.rope), layout_.tokens);
  }
  auto result = Complete(copied == cudaSuccess ? Status{}
      : Status::Fail("embedding input copy failed"), stream);
  if (copied != cudaSuccess && result.device_error == cudaSuccess)
    result.device_error = copied;
  started_ = result.ok();
  failed_ = !result.ok();
  return result;
}

EmbeddingComputeResult EmbeddingRun::NextLayer(cudaStream_t stream) {
  if (!started_ || failed_ || finished_ || completed_layers_ >= C::kLayers)
    return Invalid("embedding layer execution out of order");
  const auto& w = weights_->layers[completed_layers_];
  const int t = layout_.tokens, p = layout_.padded_tokens;
  const auto at = [&](size_t offset) {
    return reinterpret_cast<uint16_t*>(workspace_ + offset);
  };
  auto* hidden = at(layout_.hidden);
  auto* normalized = at(layout_.normalized);
  auto* projected = at(layout_.projected);
  auto* q = at(layout_.q);
  auto* k = at(layout_.k);
  auto* v = at(layout_.v);
  auto* head_q = at(layout_.head_q);
  auto* head_k = at(layout_.head_k);
  auto* vt = at(layout_.transposed_v);
  auto* scores = at(layout_.scores);
  auto* head_output = at(layout_.head_output);
  auto* attention = at(layout_.attention);
  auto* gate = at(layout_.gate);
  auto* up = at(layout_.up);
#ifdef Q4T_EMBEDDING_TRACE
  const auto trace = [&](const char* name, const uint16_t* pointer, size_t count) {
    TraceEmbeddingOperand(completed_layers_, name, pointer, count);
  };
  const auto trace_head = [&](const char* name, int head,
                              const uint16_t* pointer, size_t count) {
    trace((std::string(name) + std::to_string(head)).c_str(), pointer, count);
  };
#else
  const auto trace = [](const char*, const uint16_t*, size_t) {};
  const auto trace_head = [](const char*, int, const uint16_t*, size_t) {};
#endif
  Status status;
  const auto gemm = [&](const uint16_t* x, const uint16_t* weight, uint16_t* y,
                         int n, int inner) {
    if (!status) return;
    const auto r = Bf16Gemm(x, weight, y, t, n, inner, 1.0f, 0.0f,
        workspace_ + layout_.gemm, EmbeddingWorkspaceLayout::kGemmBytes, stream);
    if (!r.has_algo || r.status != CUBLAS_STATUS_SUCCESS)
      status = Status::Fail("embedding GEMM failed: " +
                            std::to_string(static_cast<int>(r.status)));
  };
  Norm<<<t, kThreads, 0, stream>>>(hidden, w.input_norm, normalized);
  trace("input_norm", normalized, t * C::kHidden);
  gemm(normalized, w.q, q, C::kQueryHeads * C::kHeadDim, C::kHidden);
  gemm(normalized, w.k, k, C::kKvHeads * C::kHeadDim, C::kHidden);
  gemm(normalized, w.v, v, C::kKvHeads * C::kHeadDim, C::kHidden);
  trace("q", q, t * C::kQueryHeads * C::kHeadDim);
  trace("k", k, t * C::kKvHeads * C::kHeadDim);
  trace("v", v, t * C::kKvHeads * C::kHeadDim);
  if (status) {
    NormRope<<<dim3(p, C::kQueryHeads), C::kHeadDim, 0, stream>>>(
        q, w.q_norm, at(layout_.rope), head_q, t, p, C::kQueryHeads);
    NormRope<<<dim3(p, C::kKvHeads), C::kHeadDim, 0, stream>>>(
        k, w.k_norm, at(layout_.rope), head_k, t, p, C::kKvHeads);
    const int values = p * C::kKvHeads * C::kHeadDim;
    TransposeValues<<<(values + kThreads - 1) / kThreads, kThreads, 0, stream>>>(
        v, vt, t, p);
    trace("head_q", head_q, p * C::kQueryHeads * C::kHeadDim);
    trace("head_k", head_k, p * C::kKvHeads * C::kHeadDim);
    trace("transposed_v", vt, values);
    trace("rope", at(layout_.rope), t * C::kHeadDim);
  }
  for (int h = 0; status && h < C::kQueryHeads; ++h) {
    const int kv = h / (C::kQueryHeads / C::kKvHeads);
    gemm(head_q + h * p * C::kHeadDim, head_k + kv * p * C::kHeadDim,
         scores, p, C::kHeadDim);
    if (!status) break;
    trace_head("qk_", h, scores, t * p);
    CausalSoftmax<<<t, kThreads, 0, stream>>>(scores, p);
    trace_head("probabilities_", h, scores, t * p);
    gemm(scores, vt + kv * C::kHeadDim * p, head_output, C::kHeadDim, p);
    if (!status) break;
    trace_head("pv_", h, head_output, t * C::kHeadDim);
    const int count = t * C::kHeadDim;
    MergeHead<<<(count + kThreads - 1) / kThreads, kThreads, 0, stream>>>(
        head_output, attention, t, h);
  }
  gemm(attention, w.o, projected, C::kHidden, C::kQueryHeads * C::kHeadDim);
  trace("attention", attention, t * C::kQueryHeads * C::kHeadDim);
  trace("attention_projected", projected, t * C::kHidden);
  const int hidden_count = t * C::kHidden;
  const int hidden_blocks = (hidden_count + kThreads - 1) / kThreads;
  if (status) {
    Residual<<<hidden_blocks, kThreads, 0, stream>>>(hidden, projected,
                                                   hidden_count);
    trace("attention_residual", hidden, hidden_count);
    Norm<<<t, kThreads, 0, stream>>>(hidden, w.post_norm, normalized);
    trace("post_norm", normalized, hidden_count);
  }
  gemm(normalized, w.gate, gate, C::kIntermediate, C::kHidden);
  gemm(normalized, w.up, up, C::kIntermediate, C::kHidden);
  trace("gate_raw", gate, t * C::kIntermediate);
  trace("up", up, t * C::kIntermediate);
  if (status) {
    const int count = t * C::kIntermediate;
    SwiGlu<<<(count + kThreads - 1) / kThreads, kThreads, 0, stream>>>(
        gate, up, count);
    trace("gate", gate, count);
  }
  gemm(gate, w.down, projected, C::kHidden, C::kIntermediate);
  trace("projected", projected, t * C::kHidden);
  if (status)
    Residual<<<hidden_blocks, kThreads, 0, stream>>>(hidden, projected,
                                                   hidden_count);
  auto result = Complete(status, stream);
  if (result.ok()) ++completed_layers_;
  else failed_ = true;
  return result;
}

EmbeddingComputeResult EmbeddingRun::Finish(std::span<uint16_t> last_hidden,
                                           cudaStream_t stream) {
  if (!started_ || failed_ || finished_ || completed_layers_ != C::kLayers ||
      last_hidden.size() != C::kHidden)
    return Invalid("embedding final state or output size differs");
  const auto* last = reinterpret_cast<uint16_t*>(workspace_ + layout_.hidden) +
                    static_cast<size_t>(layout_.tokens - 1) * C::kHidden;
  auto* normalized = reinterpret_cast<uint16_t*>(workspace_ + layout_.normalized);
  Norm<<<1, kThreads, 0, stream>>>(last, weights_->final_norm, normalized);
  const auto copied = cudaMemcpyAsync(last_hidden.data(), normalized,
      last_hidden.size_bytes(), cudaMemcpyDeviceToHost, stream);
  auto result = Complete(copied == cudaSuccess ? Status{}
      : Status::Fail("embedding hidden readback failed"), stream);
  if (copied != cudaSuccess && result.device_error == cudaSuccess)
    result.device_error = copied;
  finished_ = result.ok();
  failed_ = !result.ok();
  return result;
}

}  // namespace q4t::model
