#include "tts_codec_internal.h"

#include <math_constants.h>

#include "tts_math_internal.h"

namespace q4t::audio::codec_detail {
namespace {
using namespace tts_detail;

__global__ void AffineOutput(float* values, const float* bias, int count,
                             int width, bool gelu) {
  const int i = blockIdx.x * blockDim.x + threadIdx.x;
  if (i >= count) return;
  const float x = __fadd_rn(values[i], bias[i % width]);
  values[i] = gelu ? __fmul_rn(__fmul_rn(0.5f, x),
                               1.0f + erff(__fmul_rn(x, 0.7071067811865475f)))
                   : x;
}

__global__ void Normalize(float* values, const float* weight, const float* bias,
                          int width) {
  __shared__ float reduction[kThreads];
  const size_t base = static_cast<size_t>(blockIdx.x) * width;
  float sum = 0;
  for (int i = threadIdx.x; i < width; i += blockDim.x) sum += values[base + i];
  const float mean = Reduce<false>(sum, reduction) / width;
  __syncthreads();
  sum = 0;
  for (int i = threadIdx.x; i < width; i += blockDim.x) {
    const float delta = values[base + i] - mean;
    sum += __fmul_rn(delta, delta);
  }
  const float inverse = rsqrtf(Reduce<false>(sum, reduction) / width + 1e-6f);
  for (int i = threadIdx.x; i < width; i += blockDim.x)
    values[base + i] = __fadd_rn(
        __fmul_rn(__fmul_rn(values[base + i] - mean, inverse), weight[i]),
        bias[i]);
}

__global__ void Rms(const float* input, const float* weight, float* output) {
  __shared__ float reduction[kThreads];
  const size_t base = static_cast<size_t>(blockIdx.x) * 512;
  float sum = 0;
  for (int d = threadIdx.x; d < 512; d += blockDim.x)
    sum += __fmul_rn(input[base + d], input[base + d]);
  const float scale = rsqrtf(Reduce<false>(sum, reduction) / 512 + 1e-5f);
  for (int d = threadIdx.x; d < 512; d += blockDim.x)
    output[base + d] = __fmul_rn(__fmul_rn(input[base + d], scale), weight[d]);
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
  const int row = i / 1024, h = i % 1024 / 64, d = i % 64;
  const int pair = (d + 32) % 64;
  const float other =
      input[row * 1024 + h * 64 + pair] * (d < 32 ? -1.0f : 1.0f);
  output[(static_cast<size_t>(h) * stride + row) * 64 + d] =
      __fadd_rn(__fmul_rn(input[i], rope[row * 64 + d % 32]),
                __fmul_rn(other, rope[row * 64 + 32 + d % 32]));
}

__global__ void TransposeValues(const float* input, float* output, int count,
                                int stride) {
  const int i = blockIdx.x * blockDim.x + threadIdx.x;
  if (i >= count) return;
  const int row = i / 1024, h = i % 1024 / 64, d = i % 64;
  output[(h * 64 + d) * stride + row] = input[i];
}

__global__ void Softmax(float* scores, int stride) {
  __shared__ float reduction[kThreads];
  const int row = blockIdx.x;
  const int first = max(0, row - 71);
  const size_t base = static_cast<size_t>(row) * stride;
  float maximum = -CUDART_INF_F;
  for (int j = threadIdx.x; j < stride; j += blockDim.x) {
    const float x =
        j >= first && j <= row ? scores[base + j] * 0.125f : -CUDART_INF_F;
    scores[base + j] = x;
    maximum = fmaxf(maximum, x);
  }
  maximum = Reduce<true>(maximum, reduction);
  __syncthreads();
  float sum = 0;
  for (int j = first + threadIdx.x; j <= row; j += blockDim.x)
    sum += expf(scores[base + j] - maximum);
  sum = Reduce<false>(sum, reduction);
  for (int j = threadIdx.x; j < stride; j += blockDim.x)
    scores[base + j] =
        j >= first && j <= row ? expf(scores[base + j] - maximum) / sum : 0;
}

__global__ void Merge(const float* head, float* output, int count, int h) {
  const int i = blockIdx.x * blockDim.x + threadIdx.x;
  if (i < count) output[i / 64 * 1024 + h * 64 + i % 64] = head[i];
}

__global__ void Residual(float* hidden, const float* update, const float* scale,
                         int count) {
  const int i = blockIdx.x * blockDim.x + threadIdx.x;
  if (i < count)
    hidden[i] = __fadd_rn(hidden[i], __fmul_rn(update[i], scale[i % 512]));
}

__global__ void SwiGlu(float* gate, const float* up, int count) {
  const int i = blockIdx.x * blockDim.x + threadIdx.x;
  if (i < count) gate[i] = __fmul_rn(gate[i] / (1.0f + expf(-gate[i])), up[i]);
}
}  // namespace

TtsComputeResult Affine(const float* input, TtsAffineWeights weights,
                        float* output, int rows, int columns, int inner,
                        bool gelu, void* gemm, cudaStream_t stream) {
  auto status =
      Matmul(input, weights.weight, output, rows, columns, inner, inner,
             CUDA_R_32F, CUDA_R_32F, gemm, TtsDenseLayout::kGemmBytes, stream);
  if (status)
    AffineOutput<<<(rows * columns + kThreads - 1) / kThreads, kThreads, 0,
                   stream>>>(output, weights.bias, rows * columns, columns,
                             gelu);
  return Complete(status, stream);
}

TtsComputeResult LayerNorm(float* values, TtsAffineWeights weights, int rows,
                           int width, cudaStream_t stream) {
  Normalize<<<rows, kThreads, 0, stream>>>(values, weights.weight, weights.bias,
                                           width);
  return Complete({}, stream);
}

TtsComputeResult TransformerBegin(const TtsCodecDeviceWeights& weights,
                                  const TtsCodecLayout& w, uint8_t* workspace,
                                  const float* input, cudaStream_t stream) {
  // Padded V entries must be zero even though their probabilities are zero.
  const auto at = [&](size_t offset) {
    return reinterpret_cast<float*>(workspace + offset);
  };
  auto copied = cudaMemsetAsync(at(w.head_k), 0,
                                w.padded_frames * 1024 * sizeof(float), stream);
  if (copied == cudaSuccess)
    copied = cudaMemsetAsync(at(w.head_v), 0,
                             w.padded_frames * 1024 * sizeof(float), stream);
  if (copied != cudaSuccess) return Complete({}, stream, copied);
  Rope<<<(w.frames * 32 + kThreads - 1) / kThreads, kThreads, 0, stream>>>(
      at(w.rope), w.frames);
  return Affine(input, weights.input_projection, at(w.hidden), w.frames, 512,
                1024, false, workspace + w.gemm, stream);
}

TtsComputeResult TransformerLayer(const TtsCodecDeviceWeights& weights,
                                  const TtsCodecLayout& w, uint8_t* workspace,
                                  int layer, cudaStream_t stream) {
  if (layer < 0 || layer >= 8)
    return Invalid("TTS codec transformer layer invalid");
  const auto& v = weights.layers[layer];
  const int rows = w.frames, stride = w.padded_frames;
  const auto at = [&](size_t offset) {
    return reinterpret_cast<float*>(workspace + offset);
  };
  Status status;
  const auto gemm = [&](const float* x, const float* weight, float* output,
                        int n, int k, int leading = 0) {
    if (status)
      status = Matmul(x, weight, output, rows, n, k, leading ? leading : k,
                      CUDA_R_32F, CUDA_R_32F, workspace + w.gemm,
                      TtsDenseLayout::kGemmBytes, stream);
  };
  Rms<<<rows, kThreads, 0, stream>>>(at(w.hidden), v.input_norm,
                                     at(w.normalized));
  gemm(at(w.normalized), v.q, at(w.q), 1024, 512);
  gemm(at(w.normalized), v.k, at(w.k), 1024, 512);
  gemm(at(w.normalized), v.v, at(w.v), 1024, 512);
  if (status) {
    const int blocks = (rows * 1024 + kThreads - 1) / kThreads;
    Rotate<<<blocks, kThreads, 0, stream>>>(at(w.q), at(w.rope), at(w.head_q),
                                            rows * 1024, stride);
    Rotate<<<blocks, kThreads, 0, stream>>>(at(w.k), at(w.rope), at(w.head_k),
                                            rows * 1024, stride);
    TransposeValues<<<blocks, kThreads, 0, stream>>>(at(w.v), at(w.head_v),
                                                     rows * 1024, stride);
  }
  for (int h = 0; status && h < 16; ++h) {
    gemm(at(w.head_q) + h * stride * 64, at(w.head_k) + h * stride * 64,
         at(w.scores), stride, 64);
    if (!status) break;
    Softmax<<<rows, kThreads, 0, stream>>>(at(w.scores), stride);
    gemm(at(w.scores), at(w.head_v) + h * stride * 64, at(w.head_output), 64,
         stride);
    if (status)
      Merge<<<(rows * 64 + kThreads - 1) / kThreads, kThreads, 0, stream>>>(
          at(w.head_output), at(w.attention), rows * 64, h);
  }
  gemm(at(w.attention), v.o, at(w.projected), 512, 1024);
  if (status) {
    Residual<<<(rows * 512 + kThreads - 1) / kThreads, kThreads, 0, stream>>>(
        at(w.hidden), at(w.projected), v.attention_scale, rows * 512);
    Rms<<<rows, kThreads, 0, stream>>>(at(w.hidden), v.post_norm,
                                       at(w.normalized));
  }
  gemm(at(w.normalized), v.gate, at(w.gate), 1024, 512);
  gemm(at(w.normalized), v.up, at(w.up), 1024, 512);
  if (status)
    SwiGlu<<<(rows * 1024 + kThreads - 1) / kThreads, kThreads, 0, stream>>>(
        at(w.gate), at(w.up), rows * 1024);
  gemm(at(w.gate), v.down, at(w.projected), 512, 1024);
  if (status)
    Residual<<<(rows * 512 + kThreads - 1) / kThreads, kThreads, 0, stream>>>(
        at(w.hidden), at(w.projected), v.mlp_scale, rows * 512);
  return Complete(status, stream);
}

TtsComputeResult TransformerFinish(const TtsCodecDeviceWeights& weights,
                                   const TtsCodecLayout& w, uint8_t* workspace,
                                   float* output, cudaStream_t stream) {
  const auto at = [&](size_t offset) {
    return reinterpret_cast<float*>(workspace + offset);
  };
  Rms<<<w.frames, kThreads, 0, stream>>>(at(w.hidden), weights.final_norm,
                                         at(w.normalized));
  return Affine(at(w.normalized), weights.output_projection, output, w.frames,
                1024, 512, false, workspace + w.gemm, stream);
}

}  // namespace q4t::audio::codec_detail
