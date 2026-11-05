#include "q4t/mtp/quantized_head.h"

#include <cuda_bf16.h>
#include <cuda_fp4.h>
#include <cuda_fp8.h>
#include <math_constants.h>

#include <algorithm>
#include <array>
#include <cmath>
#include <cstdio>
#include <cstring>

#include "q4t/runtime/cleanup_policy.h"

namespace q4t::mtp {
namespace {

constexpr int kThreads = 256;
constexpr int kMaximumBlocks = 1024;
constexpr size_t kElements = size_t{kQuantizedHeadVocab} * kQuantizedHeadHidden;

__global__ void MaximumKernel(const uint16_t* source, float* maxima) {
  float value = 0.0f;
  for (size_t i = size_t{blockIdx.x} * blockDim.x + threadIdx.x; i < kElements;
       i += size_t{gridDim.x} * blockDim.x) {
    const float weight = __bfloat162float(__ushort_as_bfloat16(source[i]));
    // fmax alone would hide NaNs. Propagate any nonfinite value as +infinity.
    value = fmaxf(value, isfinite(weight) ? fabsf(weight) : CUDART_INF_F);
  }
  __shared__ float partial[kThreads];
  partial[threadIdx.x] = value;
  __syncthreads();
  for (int stride = kThreads / 2; stride > 0; stride /= 2) {
    if (threadIdx.x < stride)
      partial[threadIdx.x] =
          fmaxf(partial[threadIdx.x], partial[threadIdx.x + stride]);
    __syncthreads();
  }
  if (threadIdx.x == 0) maxima[blockIdx.x] = partial[0];
}

// One lane owns one 16-element group. No full BF16/FP32 shadow is allocated.
__global__ void QuantizeKernel(const uint16_t* source, uint8_t* packed,
                               uint8_t* scales, float global_scale) {
  const size_t group = size_t{blockIdx.x} * blockDim.x + threadIdx.x;
  if (group >= kElements / 16) return;
  float values[16];
  float maximum = 0.0f;
#pragma unroll
  for (int j = 0; j < 16; ++j) {
    values[j] = __bfloat162float(__ushort_as_bfloat16(source[group * 16 + j]));
    maximum = fmaxf(maximum, fabsf(values[j]));
  }
  const float raw_scale = fminf(448.0f, (maximum / 6.0f) / global_scale);
  const __nv_fp8_e4m3 scale(raw_scale);
  scales[group] = scale.__x;
  const float multiplier = static_cast<float>(scale) * global_scale;
  const float reciprocal = multiplier == 0.0f ? 0.0f : 1.0f / multiplier;
#pragma unroll
  for (int j = 0; j < 8; ++j) {
    const float2 pair = {values[2 * j] * reciprocal,
                         values[2 * j + 1] * reciprocal};
    packed[group * 8 + j] =
        __nv_cvt_float2_to_fp4x2(pair, __NV_E2M1, cudaRoundNearest);
  }
}

__device__ __forceinline__ void Accumulate(uint64_t& acc, float2 x, float2 w) {
  uint64_t x_bits, w_bits;
  memcpy(&x_bits, &x, sizeof(x));
  memcpy(&w_bits, &w, sizeof(w));
  asm volatile("fma.rn.f32x2 %0, %1, %2, %0;"
               : "+l"(acc)
               : "l"(x_bits), "l"(w_bits));
}

// Each row has the same reduction, including at M=1/2/4. Packed weights are
// consumed directly; no global dequantization or per-call allocation/sync.
__global__ void ForwardKernel(const uint8_t* packed, const uint8_t* scales,
                              float global_scale, const uint16_t* input,
                              uint16_t* output) {
  constexpr int kHidden = kQuantizedHeadHidden;
  constexpr int kWarps = kThreads / 32;
  constexpr int kBlocks = (kQuantizedHeadVocab + kWarps - 1) / kWarps;
  const int n = blockIdx.x + (threadIdx.x / 32) * kBlocks;
  const int lane = threadIdx.x % 32;
  const uint16_t* row = input + size_t{blockIdx.y} * kHidden;
  __shared__ __align__(16) uint16_t x[kHidden];
  for (int i = threadIdx.x; i < kHidden; i += kThreads) x[i] = row[i];
  __syncthreads();
  if (n >= kQuantizedHeadVocab) return;
  const uint32_t* weight = reinterpret_cast<const uint32_t*>(
      packed + static_cast<size_t>(n) * kHidden / 2);
  const uint8_t* scale_row = scales + static_cast<size_t>(n) * kHidden / 16;
  const float4* x8 = reinterpret_cast<const float4*>(x);
  uint64_t acc = 0;
  for (int i = lane; i < kHidden / 8; i += 32) {
    const uint32_t bits = weight[i];
    __nv_fp8_e4m3 block_scale;
    block_scale.__x = scale_row[i / 2];
    const float scale = static_cast<float>(block_scale) * global_scale;
    const float4 xr = x8[i];
    const auto* x2 = reinterpret_cast<const __nv_bfloat162*>(&xr);
#pragma unroll
    for (int j = 0; j < 4; ++j) {
      const __half2 raw = __nv_cvt_fp4x2_to_halfraw2(
          static_cast<uint8_t>(bits >> (8 * j)), __NV_E2M1);
      const float2 decoded = __half22float2(raw);
      // Explicit W4A16 intermediate rounding, shared by all row counts.
      const __nv_bfloat162 rounded =
          __floats2bfloat162_rn(decoded.x * scale, decoded.y * scale);
      Accumulate(acc, __bfloat1622float2(x2[j]), __bfloat1622float2(rounded));
    }
  }
  float2 sums;
  memcpy(&sums, &acc, sizeof(sums));
  float sum = sums.x + sums.y;
#pragma unroll
  for (int offset = 16; offset > 0; offset /= 2)
    sum += __shfl_down_sync(0xffffffff, sum, offset);
  if (lane == 0)
    output[size_t{blockIdx.y} * kQuantizedHeadVocab + n] =
        __bfloat16_as_ushort(__float2bfloat16_rn(sum));
}

}  // namespace

void QuantizedHead::Free() {
  if (packed) runtime::RequireCleanupSuccess(cudaFree(packed), "draft head");
  if (scales) runtime::RequireCleanupSuccess(cudaFree(scales), "draft scales");
  packed = scales = nullptr;
  global_scale = 0.0f;
}

Status BuildQuantizedHead(const uint16_t* source, QuantizedHead* output,
                          cudaStream_t stream) {
  if (!source || !output || output->packed || output->scales)
    return Status::Fail("NVFP4 draft head: invalid source or occupied output");
  QuantizedHead pending;
  float* device_maxima = nullptr;
  std::array<float, kMaximumBlocks> maxima;
  struct Drain {
    QuantizedHead* pending;
    float** maxima;
    cudaStream_t stream;
    ~Drain() {
      runtime::RequireCleanupSuccess(cudaStreamSynchronize(stream),
                                     "quantized head load drain");
      if (*maxima)
        runtime::RequireCleanupSuccess(cudaFree(*maxima), "head maxima");
      pending->Free();
    }
  } drain{&pending, &device_maxima, stream};
  if (cudaMalloc(reinterpret_cast<void**>(&device_maxima),
                 kQuantizedHeadTemporaryBytes) != cudaSuccess)
    return Status::Fail("NVFP4 draft head: maximum allocation failed");
  MaximumKernel<<<kMaximumBlocks, kThreads, 0, stream>>>(source, device_maxima);
  if (cudaGetLastError() != cudaSuccess)
    return Status::Fail("NVFP4 draft head: maximum launch failed");
  if (cudaMemcpyAsync(maxima.data(), device_maxima, sizeof(maxima),
                      cudaMemcpyDeviceToHost, stream) != cudaSuccess ||
      cudaStreamSynchronize(stream) != cudaSuccess)
    return Status::Fail("NVFP4 draft head: maximum read failed");
  const float maximum = *std::max_element(maxima.begin(), maxima.end());
  if (!std::isfinite(maximum))
    return Status::Fail("NVFP4 draft head: nonfinite source weight");
  pending.global_scale = std::max(maximum / (6.0f * 448.0f), 1e-12f);
  if (cudaMalloc(reinterpret_cast<void**>(&pending.packed),
                 kQuantizedHeadPackedBytes) != cudaSuccess ||
      cudaMalloc(reinterpret_cast<void**>(&pending.scales),
                 kQuantizedHeadScaleBytes) != cudaSuccess)
    return Status::Fail("NVFP4 draft head: weight allocation failed");
  const int blocks = (kElements / 16 + kThreads - 1) / kThreads;
  QuantizeKernel<<<blocks, kThreads, 0, stream>>>(
      source, pending.packed, pending.scales, pending.global_scale);
  if (cudaGetLastError() != cudaSuccess ||
      cudaStreamSynchronize(stream) != cudaSuccess)
    return Status::Fail("NVFP4 draft head: quantization failed");
  *output = pending;
  pending = {};
  std::fprintf(
      stderr,
      "[q4t] NVFP4 draft head W4A16 rows=%d hidden=%d bytes=%zu g=%.9g\n",
      kQuantizedHeadVocab, kQuantizedHeadHidden, kQuantizedHeadBytes,
      output->global_scale);
  return Status();
}

Status QuantizedHeadForward(const QuantizedHead& head, const uint16_t* input,
                            uint16_t* output, int rows, cudaStream_t stream) {
  if (!head.packed || !head.scales || !input || !output || rows < 1 ||
      rows > 8192 || !std::isfinite(head.global_scale) ||
      head.global_scale <= 0.0f)
    return Status::Fail("NVFP4 draft head: invalid forward arguments");
  const dim3 grid((kQuantizedHeadVocab + kThreads / 32 - 1) / (kThreads / 32),
                  rows);
  ForwardKernel<<<grid, kThreads, 0, stream>>>(
      head.packed, head.scales, head.global_scale, input, output);
  return cudaGetLastError() == cudaSuccess
             ? Status()
             : Status::Fail("NVFP4 draft head: forward launch failed");
}

}  // namespace q4t::mtp
