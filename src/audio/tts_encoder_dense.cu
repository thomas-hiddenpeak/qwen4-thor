#include "q4t/audio/tts_encoder.h"

#include <math_constants.h>

#include "tts_math_internal.h"

namespace q4t::audio {
namespace {
using namespace tts_detail;

__global__ void Normalize(const float* input, const float* weight,
                          const float* bias, float* output) {
  __shared__ float reduction[kThreads];
  const size_t base = static_cast<size_t>(blockIdx.x) * 512;
  float sum = 0;
  for (int d = threadIdx.x; d < 512; d += blockDim.x) sum += input[base + d];
  const float mean = Reduce<false>(sum, reduction) / 512;
  __syncthreads();
  sum = 0;
  for (int d = threadIdx.x; d < 512; d += blockDim.x) {
    const float delta = input[base + d] - mean;
    sum += __fmul_rn(delta, delta);
  }
  const float inverse = rsqrtf(Reduce<false>(sum, reduction) / 512 + 1e-5f);
  for (int d = threadIdx.x; d < 512; d += blockDim.x)
    output[base + d] = __fadd_rn(
        __fmul_rn(__fmul_rn(input[base + d] - mean, inverse), weight[d]),
        bias[d]);
}

__global__ void Rope(float* output, int rows) {
  const int i = blockIdx.x * blockDim.x + threadIdx.x;
  if (i >= rows * 32) return;
  const int row = i / 32, d = i % 32;
  const float inverse = 1.0f / powf(10000.0f, 2.0f * d / 64);
  const float angle = __fmul_rn(static_cast<float>(row), inverse);
  output[row * 64 + d] = cosf(angle);
  output[row * 64 + 32 + d] = sinf(angle);
}

__global__ void Rotate(const float* input, const float* rope, float* output,
                       int count, int stride) {
  const int i = blockIdx.x * blockDim.x + threadIdx.x;
  if (i >= count) return;
  const int row = i / 512, head = i % 512 / 64, d = i % 64;
  const float other =
      input[row * 512 + head * 64 + (d + 32) % 64] * (d < 32 ? -1.0f : 1.0f);
  output[(static_cast<size_t>(head) * stride + row) * 64 + d] =
      __fadd_rn(__fmul_rn(input[i], rope[row * 64 + d % 32]),
                __fmul_rn(other, rope[row * 64 + 32 + d % 32]));
}

__global__ void TransposeValues(const float* input, float* output, int count,
                                int stride) {
  const int i = blockIdx.x * blockDim.x + threadIdx.x;
  if (i >= count) return;
  const int row = i / 512, head = i % 512 / 64, d = i % 64;
  output[(head * 64 + d) * stride + row] = input[i];
}

__global__ void Softmax(float* scores, int stride) {
  __shared__ float reduction[kThreads];
  const int row = blockIdx.x;
  const size_t base = static_cast<size_t>(row) * stride;
  float maximum = -CUDART_INF_F;
  for (int j = threadIdx.x; j < stride; j += blockDim.x) {
    const float x =
        j <= row ? __fmul_rn(scores[base + j], 0.125f) : -CUDART_INF_F;
    scores[base + j] = x;
    maximum = fmaxf(maximum, x);
  }
  maximum = Reduce<true>(maximum, reduction);
  __syncthreads();
  float sum = 0;
  for (int j = threadIdx.x; j <= row; j += blockDim.x)
    sum += expf(scores[base + j] - maximum);
  sum = Reduce<false>(sum, reduction);
  for (int j = threadIdx.x; j < stride; j += blockDim.x)
    scores[base + j] = j <= row ? expf(scores[base + j] - maximum) / sum : 0;
}

__global__ void Merge(const float* head, float* output, int count, int h) {
  const int i = blockIdx.x * blockDim.x + threadIdx.x;
  if (i < count) output[i / 64 * 512 + h * 64 + i % 64] = head[i];
}

__global__ void Residual(float* hidden, const float* update, const float* scale,
                         int count) {
  const int i = blockIdx.x * blockDim.x + threadIdx.x;
  if (i < count)
    hidden[i] = __fadd_rn(hidden[i], __fmul_rn(update[i], scale[i % 512]));
}

__global__ void Gelu(float* values, int count) {
  const int i = blockIdx.x * blockDim.x + threadIdx.x;
  if (i < count) {
    const float x = values[i];
    values[i] =
        __fmul_rn(__fmul_rn(x, 0.5f),
                  __fadd_rn(1.0f, erff(__fmul_rn(x, 0.7071067811865475244f))));
  }
}

__global__ void NormalizeBook(const float* sums, const float* usage,
                              const float* initialized, float* output,
                              int* invalid) {
  const int i = blockIdx.x * blockDim.x + threadIdx.x;
  if (i >= 2048 * 256) return;
  const float count = usage[i / 256], sum = sums[i];
  const float value = sum / fmaxf(count, 1e-5f);
  if (initialized[0] != 1 || !isfinite(count) || count < 0 || !isfinite(sum) ||
      !isfinite(value)) {
    atomicExch(invalid, 1);
    output[i] = 0;
  } else {
    output[i] = value;
  }
}

// Squared Euclidean distance preserves nearest-centroid order. Reduce actual
// FP32 differences, without TF32 or cancellation-prone norm subtraction.
__global__ void Distances(const float* input, const float* book, float* output,
                          int* invalid) {
  __shared__ float reduction[kThreads];
  const int row = blockIdx.x / 2048, code = blockIdx.x % 2048;
  const int d = threadIdx.x;
  const float delta = input[row * 256 + d] - book[code * 256 + d];
  const float sum = Reduce<false>(__fmul_rn(delta, delta), reduction);
  if (threadIdx.x == 0) {
    if (!isfinite(sum)) atomicExch(invalid, 1);
    output[blockIdx.x] = isfinite(sum) ? sum : CUDART_INF_F;
  }
}

__global__ void SelectAndSubtract(float* residual, const float* book,
                                  const float* distances, int32_t* codes,
                                  int group, int* invalid) {
  __shared__ float best[kThreads];
  __shared__ int choices[kThreads];
  const int row = blockIdx.x, d = threadIdx.x;
  float minimum = CUDART_INF_F;
  int chosen = 2048;
  for (int code = d; code < 2048; code += blockDim.x) {
    const float value = distances[row * 2048 + code];
    if (value < minimum || (value == minimum && code < chosen)) {
      minimum = value;
      chosen = code;
    }
  }
  best[d] = minimum;
  choices[d] = chosen;
  __syncthreads();
  for (int n = blockDim.x / 2; n; n /= 2) {
    if (d < n && (best[d + n] < best[d] ||
                  (best[d + n] == best[d] && choices[d + n] < choices[d]))) {
      best[d] = best[d + n];
      choices[d] = choices[d + n];
    }
    __syncthreads();
  }
  if (!isfinite(best[0]) || choices[0] >= 2048) {
    if (d == 0) atomicExch(invalid, 1);
    return;
  }
  if (d == 0) codes[row * 16 + group] = choices[0];
  residual[row * 256 + d] =
      __fsub_rn(residual[row * 256 + d], book[choices[0] * 256 + d]);
  if (!isfinite(residual[row * 256 + d])) atomicExch(invalid, 1);
}
}  // namespace

TtsComputeResult TtsEncoderRun::TransformerStart(cudaStream_t stream) {
  if (rows_ != layout_.transformer_rows || width_ != 512)
    return Invalid("TTS encoder transformer shape differs");
  auto copied = cudaMemcpyAsync(At(layout_.hidden), Current(),
                                rows_ * 512 * sizeof(float),
                                cudaMemcpyDeviceToDevice, stream);
  if (copied == cudaSuccess)
    copied = cudaMemsetAsync(At(layout_.head_k), 0,
                             layout_.padded_rows * 512 * sizeof(float), stream);
  if (copied == cudaSuccess)
    copied = cudaMemsetAsync(At(layout_.head_v), 0,
                             layout_.padded_rows * 512 * sizeof(float), stream);
  if (copied != cudaSuccess) return Complete({}, stream, copied);
  Rope<<<(rows_ * 32 + kThreads - 1) / kThreads, kThreads, 0, stream>>>(
      At(layout_.rope), static_cast<int>(rows_));
  return Complete({}, stream);
}

TtsComputeResult TtsEncoderRun::AttentionProject(cudaStream_t stream) {
  const auto& layer = weights_->layers[layer_];
  Normalize<<<rows_, kThreads, 0, stream>>>(
      At(layout_.hidden), layer.input_norm.weight, layer.input_norm.bias,
      At(layout_.normalized));
  Status status;
  for (const auto& [weight, offset] : {std::pair{layer.q, layout_.q},
                                       {layer.k, layout_.k},
                                       {layer.v, layout_.v}}) {
    if (status)
      status =
          Matmul(At(layout_.normalized), weight, At(offset),
                 static_cast<int>(rows_), 512, 512, 512, CUDA_R_32F, CUDA_R_32F,
                 workspace_ + layout_.gemm, TtsDenseLayout::kGemmBytes, stream);
  }
  if (status) {
    const int count = static_cast<int>(rows_) * 512;
    const int blocks = (count + kThreads - 1) / kThreads;
    const int stride = static_cast<int>(layout_.padded_rows);
    Rotate<<<blocks, kThreads, 0, stream>>>(At(layout_.q), At(layout_.rope),
                                            At(layout_.head_q), count, stride);
    Rotate<<<blocks, kThreads, 0, stream>>>(At(layout_.k), At(layout_.rope),
                                            At(layout_.head_k), count, stride);
    TransposeValues<<<blocks, kThreads, 0, stream>>>(
        At(layout_.v), At(layout_.head_v), count, stride);
  }
  return Complete(status, stream);
}

TtsComputeResult TtsEncoderRun::AttentionHead(cudaStream_t stream) {
  const int rows = static_cast<int>(rows_);
  const int stride = static_cast<int>(layout_.padded_rows);
  const auto offset = static_cast<size_t>(head_) * stride * 64;
  auto status =
      Matmul(At(layout_.head_q) + offset, At(layout_.head_k) + offset,
             At(layout_.scores), rows, stride, 64, 64, CUDA_R_32F, CUDA_R_32F,
             workspace_ + layout_.gemm, TtsDenseLayout::kGemmBytes, stream);
  if (status) {
    // Pinned Mimi non-streaming eager calls create_causal_mask directly;
    // its declared sliding_window=250 is not applied to this execution.
    Softmax<<<rows, kThreads, 0, stream>>>(At(layout_.scores), stride);
    status = Matmul(At(layout_.scores), At(layout_.head_v) + offset,
                    At(layout_.head_output), rows, 64, stride, stride,
                    CUDA_R_32F, CUDA_R_32F, workspace_ + layout_.gemm,
                    TtsDenseLayout::kGemmBytes, stream);
  }
  if (status)
    Merge<<<(rows * 64 + kThreads - 1) / kThreads, kThreads, 0, stream>>>(
        At(layout_.head_output), At(layout_.attention), rows * 64, head_);
  return Complete(status, stream);
}

TtsComputeResult TtsEncoderRun::AttentionFinish(cudaStream_t stream) {
  const auto& layer = weights_->layers[layer_];
  const int rows = static_cast<int>(rows_);
  auto status =
      Matmul(At(layout_.attention), layer.o, At(layout_.projected), rows, 512,
             512, 512, CUDA_R_32F, CUDA_R_32F, workspace_ + layout_.gemm,
             TtsDenseLayout::kGemmBytes, stream);
  if (status)
    Residual<<<(rows * 512 + kThreads - 1) / kThreads, kThreads, 0, stream>>>(
        At(layout_.hidden), At(layout_.projected), layer.attention_scale,
        rows * 512);
  return Complete(status, stream);
}

TtsComputeResult TtsEncoderRun::FeedForward(cudaStream_t stream) {
  const auto& layer = weights_->layers[layer_];
  const int rows = static_cast<int>(rows_);
  Normalize<<<rows, kThreads, 0, stream>>>(
      At(layout_.hidden), layer.post_norm.weight, layer.post_norm.bias,
      At(layout_.normalized));
  auto status =
      Matmul(At(layout_.normalized), layer.fc1, At(layout_.feedforward), rows,
             2048, 512, 512, CUDA_R_32F, CUDA_R_32F, workspace_ + layout_.gemm,
             TtsDenseLayout::kGemmBytes, stream);
  if (status) {
    Gelu<<<(rows * 2048 + kThreads - 1) / kThreads, kThreads, 0, stream>>>(
        At(layout_.feedforward), rows * 2048);
    status =
        Matmul(At(layout_.feedforward), layer.fc2, At(layout_.projected), rows,
               512, 2048, 2048, CUDA_R_32F, CUDA_R_32F,
               workspace_ + layout_.gemm, TtsDenseLayout::kGemmBytes, stream);
  }
  if (status)
    Residual<<<(rows * 512 + kThreads - 1) / kThreads, kThreads, 0, stream>>>(
        At(layout_.hidden), At(layout_.projected), layer.mlp_scale, rows * 512);
  return Complete(status, stream);
}

TtsComputeResult TtsEncoderRun::Quantize(cudaStream_t stream) {
  const auto& w = *weights_;
  auto* invalid = reinterpret_cast<int32_t*>(workspace_ + layout_.finite_flag);
  NormalizeBook<<<2048 * 256 / kThreads, kThreads, 0, stream>>>(
      w.embedding_sum[group_], w.usage[group_], w.initialized[group_],
      At(layout_.codebook), invalid);
  Distances<<<rows_ * 2048, kThreads, 0, stream>>>(
      At(layout_.residual), At(layout_.codebook), At(layout_.distances),
      invalid);
  SelectAndSubtract<<<rows_, kThreads, 0, stream>>>(
      At(layout_.residual), At(layout_.codebook), At(layout_.distances),
      reinterpret_cast<int32_t*>(workspace_ + layout_.codes), group_, invalid);
  return Complete({}, stream);
}

}  // namespace q4t::audio
