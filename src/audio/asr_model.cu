#include "q4t/audio/asr_model.h"

#include <cuda_bf16.h>
#include <math_constants.h>

#include <algorithm>
#include <cmath>
#include <mutex>
#include <string>

#include "q4t/model/linear.h"
#include "q4t/quant/lt_cache.h"

namespace q4t::audio {
namespace {
using C = AsrConfig;
constexpr int kThreads = 256;

__device__ float Read(const uint16_t* data, size_t i) {
  return __bfloat162float(__ushort_as_bfloat16(data[i]));
}
__device__ uint16_t Write(float value) {
  return __bfloat16_as_ushort(__float2bfloat16_rn(value));
}
__device__ float Round(float value) {
  return __bfloat162float(__float2bfloat16_rn(value));
}
__device__ float Gelu(float value) {
  return __fmul_rn(__fmul_rn(0.5f, value),
                   1.0f + erff(value * 0.7071067811865475244f));
}

template <bool kMax>
__device__ float Reduce(float value, float* scratch) {
  scratch[threadIdx.x] = value;
  __syncthreads();
  for (int n = blockDim.x / 2; n; n /= 2) {
    if (threadIdx.x < n)
      scratch[threadIdx.x] =
          kMax ? fmaxf(scratch[threadIdx.x], scratch[threadIdx.x + n])
               : scratch[threadIdx.x] + scratch[threadIdx.x + n];
    __syncthreads();
  }
  return scratch[0];
}

__global__ void MelColumns(const float* mel, uint16_t* columns, int frames,
                           int start, int valid, int width) {
  const int i = blockIdx.x * blockDim.x + threadIdx.x;
  const int out_width = (width + 1) / 2;
  if (i >= 64 * out_width * 9) return;
  const int tap = i % 9, row = i / 9;
  const int f = (row / out_width) * 2 - 1 + tap / 3;
  const int t = (row % out_width) * 2 - 1 + tap % 3;
  columns[i] = Write(f >= 0 && f < C::kMelBins && t >= 0 && t < valid
                         ? mel[f * frames + start + t]
                         : 0.0f);
}

// Input/output are NHWC; the column inner dimension keeps checkpoint OIHW
// order (channel, kernel frequency, kernel time).
__global__ void ConvColumns(const uint16_t* input, uint16_t* columns,
                            int frequency, int width) {
  const int i = blockIdx.x * blockDim.x + threadIdx.x;
  const int out_width = (width + 1) / 2;
  const int inner = C::kConvChannels * 9;
  if (i >= (frequency + 1) / 2 * out_width * inner) return;
  const int row = i / inner, tap = i % 9;
  const int c = i % inner / 9;
  const int f = row / out_width * 2 - 1 + tap / 3;
  const int t = row % out_width * 2 - 1 + tap % 3;
  columns[i] = f >= 0 && f < frequency && t >= 0 && t < width
                   ? input[(f * width + t) * C::kConvChannels + c]
                   : 0;
}

__global__ void AffineOutput(const float* input, const uint16_t* bias,
                             uint16_t* output, int count, int channels,
                             bool gelu) {
  const int i = blockIdx.x * blockDim.x + threadIdx.x;
  if (i >= count) return;
  const float value =
      Round(bias ? __fadd_rn(input[i], Read(bias, i % channels)) : input[i]);
  output[i] = Write(gelu ? Gelu(value) : value);
}

__global__ void FlattenConv(const uint16_t* input, uint16_t* output, int t) {
  const int i = blockIdx.x * blockDim.x + threadIdx.x;
  constexpr int inner = C::kConvChannels * C::kConvFrequency;
  if (i >= t * inner) return;
  const int frame = i / inner, c = i % inner / C::kConvFrequency;
  const int f = i % C::kConvFrequency;
  output[i] = input[(f * t + frame) * C::kConvChannels + c];
}

__global__ void PositionAndPack(const uint16_t* input, uint16_t* hidden,
                                int tokens, int offset) {
  const int i = blockIdx.x * blockDim.x + threadIdx.x;
  if (i >= tokens * C::kEncoderHidden) return;
  const int t = i / C::kEncoderHidden, d = i % C::kEncoderHidden;
  constexpr int half = C::kEncoderHidden / 2;
  // Official table is built in FP32 and cast to BF16 before residual add.
  constexpr float log_step = 9.210340371976184f / (half - 1);
  const float angle =
      __fmul_rn(static_cast<float>(t),
                expf(__fmul_rn(-log_step, static_cast<float>(d % half))));
  const float position = Round(d < half ? sinf(angle) : cosf(angle));
  hidden[offset * C::kEncoderHidden + i] =
      Write(__fadd_rn(Read(input, i), position));
}

__global__ void LayerNorm(const uint16_t* input, const uint16_t* weight,
                          const uint16_t* bias, uint16_t* output) {
  __shared__ float reduction[kThreads];
  const int base = blockIdx.x * C::kEncoderHidden;
  float sum = 0;
  for (int d = threadIdx.x; d < C::kEncoderHidden; d += blockDim.x)
    sum += Read(input, base + d);
  const float mean = Reduce<false>(sum, reduction) / C::kEncoderHidden;
  __syncthreads();
  float variance = 0;
  for (int d = threadIdx.x; d < C::kEncoderHidden; d += blockDim.x) {
    const float x = Read(input, base + d) - mean;
    variance += __fmul_rn(x, x);
  }
  const float scale =
      rsqrtf(Reduce<false>(variance, reduction) / C::kEncoderHidden +
             C::kEncoderNormEpsilon);
  for (int d = threadIdx.x; d < C::kEncoderHidden; d += blockDim.x)
    output[base + d] = Write(
        __fadd_rn(__fmul_rn(__fmul_rn(Read(input, base + d) - mean, scale),
                            Read(weight, d)),
                  Read(bias, d)));
}

__global__ void WindowAttention(const uint16_t* q, const uint16_t* k,
                                const uint16_t* v, uint16_t* output, int tokens,
                                int window) {
  // At most 104 trained positions per window. One block owns a row/head;
  // the score and probability rounding matches masked eager BF16 attention.
  __shared__ float reduction[128];
  __shared__ float scores[128];
  const int row = blockIdx.x, head = blockIdx.y, j = threadIdx.x;
  const int start = row / window * window;
  const int length = min(window, tokens - start);
  const int query = row * C::kEncoderHidden + head * C::kEncoderHeadDim;
  float score = -CUDART_INF_F;
  if (j < length) {
    float dot = 0;
    const int key = (start + j) * C::kEncoderHidden + head * C::kEncoderHeadDim;
    for (int d = 0; d < C::kEncoderHeadDim; ++d)
      dot = fmaf(Read(q, query + d), Read(k, key + d), dot);
    score = Round(__fmul_rn(Round(dot), 0.125f));
  }
  scores[j] = score;
  const float maximum = Reduce<true>(score, reduction);
  __syncthreads();
  const float p = j < length ? expf(score - maximum) : 0.0f;
  const float total = Reduce<false>(p, reduction);
  scores[j] = Round(p / total);
  __syncthreads();
  if (j < C::kEncoderHeadDim) {
    float value = 0;
    for (int t = 0; t < length; ++t)
      value = fmaf(scores[t],
                   Read(v, (start + t) * C::kEncoderHidden +
                               head * C::kEncoderHeadDim + j),
                   value);
    output[query + j] = Write(value);
  }
}

__global__ void Residual(uint16_t* hidden, const uint16_t* update, int count) {
  const int i = blockIdx.x * blockDim.x + threadIdx.x;
  if (i < count) hidden[i] = Write(__fadd_rn(Read(hidden, i), Read(update, i)));
}

__global__ void DecoderInput(const int32_t* ids, const uint16_t* token_table,
                             const uint16_t* audio, uint16_t* output,
                             int audio_start, int audio_tokens, int count) {
  const int i = blockIdx.x * blockDim.x + threadIdx.x;
  if (i >= count) return;
  const int t = i / C::kHidden, d = i % C::kHidden;
  output[i] = t >= audio_start && t < audio_start + audio_tokens
                  ? audio[(t - audio_start) * C::kHidden + d]
                  : token_table[static_cast<size_t>(ids[t]) * C::kHidden + d];
}

struct LinearPlan {
  cublasLtMatmulDesc_t operation = nullptr;
  cublasLtMatrixLayout_t a = nullptr, b = nullptr, c = nullptr;
  cublasLtMatmulPreference_t preference = nullptr;
  ~LinearPlan() {
    if (preference) cublasLtMatmulPreferenceDestroy(preference);
    if (c) cublasLtMatrixLayoutDestroy(c);
    if (b) cublasLtMatrixLayoutDestroy(b);
    if (a) cublasLtMatrixLayoutDestroy(a);
    if (operation) cublasLtMatmulDescDestroy(operation);
  }
};

// FP32 accumulation/output lets bias be applied BEFORE the operator's BF16
// rounding. Adding bias to a previously rounded BF16 matmul changes the model.
// Scoped descriptors avoid an unbounded cache for arbitrary audio lengths.
Status Linear(const uint16_t* input, AsrAffineWeights weights, uint16_t* output,
              int m, int n, int k, float* temporary, void* workspace, bool gelu,
              cudaStream_t stream) {
  cublasStatus_t result = CUBLAS_STATUS_SUCCESS;
  {
    std::lock_guard<std::mutex> lock(quant::LtCacheMutex());
    auto handle = quant::GlobalLtHandle();
    if (!handle) return Status::Fail("ASR cuBLAS handle unavailable");
    LinearPlan plan;
    const auto check = [&](cublasStatus_t next) {
      if (result == CUBLAS_STATUS_SUCCESS) result = next;
      return next == CUBLAS_STATUS_SUCCESS;
    };
    if (!check(cublasLtMatmulDescCreate(&plan.operation, CUBLAS_COMPUTE_32F,
                                        CUDA_R_32F)) ||
        !check(cublasLtMatrixLayoutCreate(&plan.a, CUDA_R_16BF, k, n, k)) ||
        !check(cublasLtMatrixLayoutCreate(&plan.b, CUDA_R_16BF, k, m, k)) ||
        !check(cublasLtMatrixLayoutCreate(&plan.c, CUDA_R_32F, n, m, n)) ||
        !check(cublasLtMatmulPreferenceCreate(&plan.preference)))
      return Status::Fail("ASR linear descriptor creation failed");
    const cublasOperation_t transpose = CUBLAS_OP_T;
    const size_t limit = AsrEncoderLayout::kGemmBytes;
    const uint32_t reduction = CUBLASLT_REDUCTION_SCHEME_NONE;
    if (!check(cublasLtMatmulDescSetAttribute(plan.operation,
                                              CUBLASLT_MATMUL_DESC_TRANSA,
                                              &transpose, sizeof(transpose))) ||
        !check(cublasLtMatmulPreferenceSetAttribute(
            plan.preference, CUBLASLT_MATMUL_PREF_MAX_WORKSPACE_BYTES, &limit,
            sizeof(limit))) ||
        !check(cublasLtMatmulPreferenceSetAttribute(
            plan.preference, CUBLASLT_MATMUL_PREF_REDUCTION_SCHEME_MASK,
            &reduction, sizeof(reduction))))
      return Status::Fail("ASR linear descriptor configuration failed");
    const auto alignment = [](const void* pointer) {
      const auto address = reinterpret_cast<uintptr_t>(pointer);
      return static_cast<uint32_t>(
          std::min(uintptr_t{256}, address & (~address + 1)));
    };
    const uint32_t a_alignment = alignment(weights.weight);
    const uint32_t b_alignment = alignment(input);
    const uint32_t c_alignment = alignment(temporary);
    if (!check(cublasLtMatmulPreferenceSetAttribute(
            plan.preference, CUBLASLT_MATMUL_PREF_MIN_ALIGNMENT_A_BYTES,
            &a_alignment, sizeof(a_alignment))) ||
        !check(cublasLtMatmulPreferenceSetAttribute(
            plan.preference, CUBLASLT_MATMUL_PREF_MIN_ALIGNMENT_B_BYTES,
            &b_alignment, sizeof(b_alignment))) ||
        !check(cublasLtMatmulPreferenceSetAttribute(
            plan.preference, CUBLASLT_MATMUL_PREF_MIN_ALIGNMENT_C_BYTES,
            &c_alignment, sizeof(c_alignment))) ||
        !check(cublasLtMatmulPreferenceSetAttribute(
            plan.preference, CUBLASLT_MATMUL_PREF_MIN_ALIGNMENT_D_BYTES,
            &c_alignment, sizeof(c_alignment))))
      return Status::Fail("ASR linear alignment configuration failed");
    cublasLtMatmulHeuristicResult_t heuristic{};
    int count = 0;
    if (!check(cublasLtMatmulAlgoGetHeuristic(
            handle, plan.operation, plan.a, plan.b, plan.c, plan.c,
            plan.preference, 1, &heuristic, &count)) ||
        count != 1 || heuristic.state != CUBLAS_STATUS_SUCCESS)
      return Status::Fail("ASR linear algorithm unavailable");
    const float alpha = 1.0f, beta = 0.0f;
    check(cublasLtMatmul(handle, plan.operation, &alpha, weights.weight, plan.a,
                         input, plan.b, &beta, temporary, plan.c, temporary,
                         plan.c, &heuristic.algo, workspace, limit, stream));
  }
  if (result != CUBLAS_STATUS_SUCCESS)
    return Status::Fail("ASR linear failed: " +
                        std::to_string(static_cast<int>(result)));
  const int count = m * n;
  AffineOutput<<<(count + kThreads - 1) / kThreads, kThreads, 0, stream>>>(
      temporary, weights.bias, output, count, n, gelu);
  return {};
}

AsrComputeResult Invalid(const char* reason) {
  return {Status::Fail(reason), cudaSuccess};
}
AsrComputeResult Complete(Status status, cudaStream_t stream,
                          cudaError_t submitted = cudaSuccess) {
  const auto launch = cudaPeekAtLastError();
  const auto completion = cudaStreamSynchronize(stream);
  const auto error = completion != cudaSuccess ? completion
                     : launch != cudaSuccess   ? launch
                                               : submitted;
  if (error != cudaSuccess)
    return {Status::Fail(std::string("ASR CUDA failure: ") +
                         cudaGetErrorString(error)),
            error};
  return {std::move(status), cudaSuccess};
}
bool Present(AsrAffineWeights weights) {
  return weights.weight && weights.bias;
}
}  // namespace

Status BindAsrDeviceWeights(const void* data, size_t bytes,
                            const AsrWeightLayout& layout,
                            AsrDeviceWeights* out) {
  if (!data || reinterpret_cast<uintptr_t>(data) % 2 || !out ||
      bytes != layout.bytes)
    return Status::Fail("ASR device weight buffer differs");
  if (auto s = ValidateAsrWeightLayout(layout); !s) return s;
  const auto pointer = [&](const std::string& name) {
    const auto found =
        std::find_if(layout.tensors.begin(), layout.tensors.end(),
                     [&](const auto& tensor) { return tensor.name == name; });
    return reinterpret_cast<const uint16_t*>(static_cast<const uint8_t*>(data) +
                                             found->offset);
  };
  const auto affine = [&](const std::string& name) {
    return AsrAffineWeights{pointer(name + ".weight"), pointer(name + ".bias")};
  };
  AsrDeviceWeights result;
  const std::string audio = "thinker.audio_tower.";
  for (int i = 0; i < 3; ++i)
    result.convolution[i] = affine(audio + "conv2d" + std::to_string(i + 1));
  result.convolution_out = pointer(audio + "conv_out.weight");
  result.final_norm = affine(audio + "ln_post");
  result.proj1 = affine(audio + "proj1");
  result.proj2 = affine(audio + "proj2");
  for (int i = 0; i < C::kEncoderLayers; ++i) {
    const auto p = audio + "layers." + std::to_string(i) + ".";
    auto& layer = result.encoder[i];
    layer.input_norm = affine(p + "self_attn_layer_norm");
    layer.post_norm = affine(p + "final_layer_norm");
    layer.q = affine(p + "self_attn.q_proj");
    layer.k = affine(p + "self_attn.k_proj");
    layer.v = affine(p + "self_attn.v_proj");
    layer.o = affine(p + "self_attn.out_proj");
    layer.fc1 = affine(p + "fc1");
    layer.fc2 = affine(p + "fc2");
  }
  result.decoder.tokens = pointer("thinker.model.embed_tokens.weight");
  result.decoder.final_norm = pointer("thinker.model.norm.weight");
  result.lm_head = pointer("thinker.lm_head.weight");
  static_assert(C::kDecoderLayers == model::EmbeddingConfig::kLayers);
  for (int i = 0; i < C::kDecoderLayers; ++i) {
    const auto p = "thinker.model.layers." + std::to_string(i) + ".";
    auto& w = result.decoder.layers[i];
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
  *out = result;
  return {};
}

AsrComputeResult AsrEncoderRun::Begin(const AsrDeviceWeights& weights,
                                      std::span<const float> mel, int frames,
                                      void* workspace, size_t bytes,
                                      cudaStream_t stream) {
  if (started_ || failed_ || !workspace || frames <= 0 ||
      reinterpret_cast<uintptr_t>(workspace) % 256 ||
      mel.size() != static_cast<size_t>(frames) * C::kMelBins)
    return Invalid("invalid ASR encoder begin");
  if (!weights.convolution_out || !Present(weights.final_norm) ||
      !Present(weights.proj1) || !Present(weights.proj2))
    return Invalid("incomplete ASR encoder projection weights");
  for (const auto& conv : weights.convolution)
    if (!Present(conv)) return Invalid("incomplete ASR convolution weights");
  for (const auto& w : weights.encoder)
    if (!Present(w.input_norm) || !Present(w.post_norm) || !Present(w.q) ||
        !Present(w.k) || !Present(w.v) || !Present(w.o) || !Present(w.fc1) ||
        !Present(w.fc2))
      return Invalid("incomplete ASR encoder layer weights");
  for (const float value : mel)
    if (!std::isfinite(value)) return Invalid("nonfinite ASR mel value");
  const auto status = PlanAsrEncoderWorkspace(frames, frames, bytes, &layout_);
  if (!status) return {status, cudaSuccess};
  weights_ = &weights;
  workspace_ = static_cast<uint8_t*>(workspace);
  const auto copied =
      cudaMemcpyAsync(workspace_ + layout_.features, mel.data(),
                      mel.size_bytes(), cudaMemcpyHostToDevice, stream);
  auto result = Complete({}, stream, copied);
  started_ = result.ok();
  failed_ = !result.ok();
  return result;
}

AsrComputeResult AsrEncoderRun::NextChunk(cudaStream_t stream) {
  if (!started_ || failed_ || finished_ || completed_chunks_ >= layout_.chunks)
    return Invalid("ASR convolution execution out of order");
  const auto at = [&](size_t offset) {
    return reinterpret_cast<uint16_t*>(workspace_ + offset);
  };
  auto* temporary = reinterpret_cast<float*>(workspace_ + layout_.linear_fp32);
  const int start = completed_chunks_ * C::kMelChunk;
  const int valid = std::min(C::kMelChunk, layout_.frames - start);
  const int count = 64 * ((layout_.padded_frames + 1) / 2) * 9;
  MelColumns<<<(count + kThreads - 1) / kThreads, kThreads, 0, stream>>>(
      reinterpret_cast<float*>(workspace_ + layout_.features),
      at(layout_.columns), layout_.frames, start, valid, layout_.padded_frames);
  int frequency = C::kMelBins, width = layout_.padded_frames;
  Status status;
  for (int layer = 0; status && layer < 3; ++layer) {
    if (layer) {
      const auto input =
          layer == 1 ? layout_.convolution_a : layout_.convolution_b;
      const int n =
          (frequency + 1) / 2 * ((width + 1) / 2) * C::kConvChannels * 9;
      ConvColumns<<<(n + kThreads - 1) / kThreads, kThreads, 0, stream>>>(
          at(input), at(layout_.columns), frequency, width);
    }
    frequency = (frequency + 1) / 2;
    width = (width + 1) / 2;
    const auto output =
        layer == 1 ? layout_.convolution_b : layout_.convolution_a;
    status = Linear(at(layout_.columns), weights_->convolution[layer],
                    at(output), frequency * width, C::kConvChannels,
                    (layer == 0 ? 1 : C::kConvChannels) * 9, temporary,
                    workspace_ + layout_.gemm, true, stream);
  }
  if (status) {
    const int n = width * C::kConvChannels * C::kConvFrequency;
    FlattenConv<<<(n + kThreads - 1) / kThreads, kThreads, 0, stream>>>(
        at(layout_.convolution_a), at(layout_.conv_flat), width);
    status = Linear(at(layout_.conv_flat), {weights_->convolution_out, nullptr},
                    at(layout_.chunk_hidden), width, C::kEncoderHidden,
                    C::kConvChannels * C::kConvFrequency, temporary,
                    workspace_ + layout_.gemm, false, stream);
  }
  if (status) {
    const int tokens = (valid + 7) / 8;
    const int n = tokens * C::kEncoderHidden;
    PositionAndPack<<<(n + kThreads - 1) / kThreads, kThreads, 0, stream>>>(
        at(layout_.chunk_hidden), at(layout_.hidden), tokens,
        completed_chunks_ * C::kEncodedChunk);
  }
  auto result = Complete(status, stream);
  if (result.ok())
    ++completed_chunks_;
  else
    failed_ = true;
  return result;
}

AsrComputeResult AsrEncoderRun::NextLayer(cudaStream_t stream) {
  if (!started_ || failed_ || finished_ ||
      completed_chunks_ != layout_.chunks ||
      completed_layers_ >= C::kEncoderLayers)
    return Invalid("ASR encoder layer execution out of order");
  const auto at = [&](size_t offset) {
    return reinterpret_cast<uint16_t*>(workspace_ + offset);
  };
  const auto& w = weights_->encoder[completed_layers_];
  const int t = layout_.tokens, n = t * C::kEncoderHidden;
  auto* hidden = at(layout_.hidden);
  auto* normalized = at(layout_.normalized);
  Status status;
  const auto linear = [&](size_t input, AsrAffineWeights weights, size_t output,
                          int channels, int inner, bool gelu = false) {
    if (status)
      status =
          Linear(at(input), weights, at(output), t, channels, inner,
                 reinterpret_cast<float*>(workspace_ + layout_.linear_fp32),
                 workspace_ + layout_.gemm, gelu, stream);
  };
  LayerNorm<<<t, kThreads, 0, stream>>>(hidden, w.input_norm.weight,
                                        w.input_norm.bias, normalized);
  linear(layout_.normalized, w.q, layout_.q, C::kEncoderHidden,
         C::kEncoderHidden);
  linear(layout_.normalized, w.k, layout_.k, C::kEncoderHidden,
         C::kEncoderHidden);
  linear(layout_.normalized, w.v, layout_.v, C::kEncoderHidden,
         C::kEncoderHidden);
  if (status)
    WindowAttention<<<dim3(t, C::kEncoderHeads), 128, 0, stream>>>(
        at(layout_.q), at(layout_.k), at(layout_.v), at(layout_.attention), t,
        layout_.attention_window);
  linear(layout_.attention, w.o, layout_.projected, C::kEncoderHidden,
         C::kEncoderHidden);
  if (status) {
    Residual<<<(n + kThreads - 1) / kThreads, kThreads, 0, stream>>>(
        hidden, at(layout_.projected), n);
    LayerNorm<<<t, kThreads, 0, stream>>>(hidden, w.post_norm.weight,
                                          w.post_norm.bias, normalized);
  }
  linear(layout_.normalized, w.fc1, layout_.intermediate,
         C::kEncoderIntermediate, C::kEncoderHidden, true);
  linear(layout_.intermediate, w.fc2, layout_.projected, C::kEncoderHidden,
         C::kEncoderIntermediate);
  if (status)
    Residual<<<(n + kThreads - 1) / kThreads, kThreads, 0, stream>>>(
        hidden, at(layout_.projected), n);
  auto result = Complete(status, stream);
  if (result.ok())
    ++completed_layers_;
  else
    failed_ = true;
  return result;
}

AsrComputeResult AsrEncoderRun::Finish(cudaStream_t stream) {
  if (!started_ || failed_ || finished_ ||
      completed_layers_ != C::kEncoderLayers)
    return Invalid("ASR encoder finish out of order");
  const auto at = [&](size_t offset) {
    return reinterpret_cast<uint16_t*>(workspace_ + offset);
  };
  const int t = layout_.tokens;
  auto* temporary = reinterpret_cast<float*>(workspace_ + layout_.linear_fp32);
  LayerNorm<<<t, kThreads, 0, stream>>>(
      at(layout_.hidden), weights_->final_norm.weight,
      weights_->final_norm.bias, at(layout_.normalized));
  auto status =
      Linear(at(layout_.normalized), weights_->proj1, at(layout_.projected), t,
             C::kEncoderHidden, C::kEncoderHidden, temporary,
             workspace_ + layout_.gemm, true, stream);
  if (status)
    status = Linear(at(layout_.projected), weights_->proj2, at(layout_.output),
                    t, C::kHidden, C::kEncoderHidden, temporary,
                    workspace_ + layout_.gemm, false, stream);
  auto result = Complete(status, stream);
  finished_ = result.ok();
  failed_ = !result.ok();
  return result;
}

std::span<const uint16_t> AsrEncoderRun::output() const {
  if (!finished_ || failed_) return {};
  return {reinterpret_cast<const uint16_t*>(workspace_ + layout_.output),
          static_cast<size_t>(layout_.tokens) * C::kHidden};
}

AsrComputeResult AsrDecoderStep::Begin(const AsrDeviceWeights& weights,
                                       std::span<const int32_t> tokens,
                                       size_t audio_start,
                                       std::span<const uint16_t> device_audio,
                                       void* workspace, size_t bytes,
                                       cudaStream_t stream) {
  using Dense = model::EmbeddingConfig;
  static_assert(C::kHidden == Dense::kHidden &&
                C::kIntermediate == Dense::kIntermediate &&
                C::kQueryHeads == Dense::kQueryHeads &&
                C::kKvHeads == Dense::kKvHeads &&
                C::kHeadDim == Dense::kHeadDim &&
                C::kNormEpsilon == Dense::kNormEpsilon &&
                C::kRopeTheta == Dense::kRopeTheta);
  if (started_ || failed_ || !workspace ||
      reinterpret_cast<uintptr_t>(workspace) % 256 || !weights.decoder.tokens ||
      !weights.lm_head || device_audio.empty() ||
      device_audio.size() % C::kHidden || audio_start > tokens.size() ||
      device_audio.size() / C::kHidden > tokens.size() - audio_start)
    return Invalid("invalid ASR decoder begin");
  auto status =
      PlanAsrDecoderWorkspace(tokens.size(), tokens.size(), bytes, &layout_);
  if (!status) return {status, cudaSuccess};
  const size_t audio_tokens = device_audio.size() / C::kHidden;
  for (size_t i = 0; i < tokens.size(); ++i)
    if (tokens[i] < 0 || tokens[i] >= C::kVocab ||
        ((i >= audio_start && i - audio_start < audio_tokens) !=
         (tokens[i] == C::kAudioPad)))
      return Invalid("ASR decoder token or audio position differs");
  const auto source = reinterpret_cast<uintptr_t>(device_audio.data());
  const auto destination = reinterpret_cast<uintptr_t>(workspace);
  if (source % 2 || source + device_audio.size_bytes() < source ||
      destination + bytes < destination ||
      (source < destination + bytes &&
       destination < source + device_audio.size_bytes()))
    return Invalid("ASR audio input overlaps decoder workspace");
  weights_ = &weights;
  workspace_ = static_cast<uint8_t*>(workspace);
  auto* ids = reinterpret_cast<int32_t*>(workspace_ + layout_.ids);
  auto* input = reinterpret_cast<uint16_t*>(workspace_ + layout_.input);
  const auto copied = cudaMemcpyAsync(ids, tokens.data(), tokens.size_bytes(),
                                      cudaMemcpyHostToDevice, stream);
  if (copied != cudaSuccess) {
    failed_ = true;
    return Complete({}, stream, copied);
  }
  const int count = static_cast<int>(tokens.size()) * C::kHidden;
  DecoderInput<<<(count + kThreads - 1) / kThreads, kThreads, 0, stream>>>(
      ids, weights.decoder.tokens, device_audio.data(), input,
      static_cast<int>(audio_start), static_cast<int>(audio_tokens), count);
  auto result = dense_.BeginEmbeddings(
      weights.decoder, {input, static_cast<size_t>(count)},
      workspace_ + layout_.dense_offset, layout_.dense.bytes, stream);
  // BeginEmbeddings may reject a host contract before its synchronization.
  // Always drain this method's input preparation before returning ownership.
  result = Complete(result.status, stream, result.device_error);
  started_ = result.ok();
  failed_ = !result.ok();
  return result;
}

AsrComputeResult AsrDecoderStep::NextLayer(cudaStream_t stream) {
  if (!started_ || failed_ || finished_)
    return Invalid("ASR decoder layer execution out of order");
  auto result = dense_.NextLayer(stream);
  if (!result.ok()) failed_ = true;
  return result;
}

AsrComputeResult AsrDecoderStep::Finish(std::span<uint16_t> logits,
                                        cudaStream_t stream) {
  if (!started_ || failed_ || finished_ || logits.size() != C::kVocab)
    return Invalid("ASR decoder output state or size differs");
  std::array<uint16_t, C::kHidden> hidden;
  auto result = dense_.Finish(hidden, stream);
  if (!result.ok()) {
    failed_ = true;
    return result;
  }
  auto* last = reinterpret_cast<uint16_t*>(workspace_ + layout_.last);
  auto* device_logits =
      reinterpret_cast<uint16_t*>(workspace_ + layout_.logits);
  auto error = cudaMemcpyAsync(last, hidden.data(), sizeof(hidden),
                               cudaMemcpyHostToDevice, stream);
  Status status;
  if (error == cudaSuccess) {
    const auto gemm = model::Bf16Gemm(
        last, weights_->lm_head, device_logits, 1, C::kVocab, C::kHidden, 1.0f,
        0.0f, workspace_ + layout_.dense_offset + layout_.dense.gemm,
        model::EmbeddingWorkspaceLayout::kGemmBytes, stream);
    if (!gemm.has_algo || gemm.status != CUBLAS_STATUS_SUCCESS)
      status = Status::Fail("ASR output head GEMM failed");
    else
      error = cudaMemcpyAsync(logits.data(), device_logits, logits.size_bytes(),
                              cudaMemcpyDeviceToHost, stream);
  }
  result = Complete(status, stream, error);
  finished_ = result.ok();
  failed_ = !result.ok();
  return result;
}

}  // namespace q4t::audio
