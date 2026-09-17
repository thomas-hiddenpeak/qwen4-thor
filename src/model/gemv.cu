// M=1 BF16 GEMV: y[N] = sum_k x[k] * W[N, K] (row-major W [N, K], no bias).
//
// Decode (T=1) turns every BF16 projection into a GEMV. cuBLASLt's GEMM
// kernels are not bandwidth-optimal for M=1 (they tile over M and leave the
// tensor cores / DRAM underutilized), so this dedicated kernel reads W once
// at full DRAM bandwidth and accumulates in FP32. It is dispatched from
// q4t::model::Bf16Gemm when M == 1 (see linear.h); the result must match the
// cuBLASLt path to BF16 storage precision (FP32 accumulation, single
// rounding to BF16).
#include <cuda_bf16.h>
#include <cuda_fp8.h>
#include <cuda_runtime.h>

#include <cstdint>
#include <cstdlib>
#include <cstring>

#include "q4t/model/gemv.h"

namespace q4t {
namespace model {
namespace {

// f32x2 SIMD FMA: 2 FMAs per instruction on SM110a (1.97x scalar throughput).
// Accumulates {a.x*b.x, a.y*b.y} into the packed 64-bit pair.
__device__ __forceinline__ void F32x2Fma(uint64_t& acc, const float2& a,
                                         const float2& b) {
  asm volatile("fma.rn.f32x2 %0, %1, %2, %0;"
               : "+l"(acc)
               : "l"(reinterpret_cast<const uint64_t&>(a)),
                 "l"(reinterpret_cast<const uint64_t&>(b)));
}
__device__ __forceinline__ float F32x2Reduce(uint64_t acc) {
  float2 v;
  std::memcpy(&v, &acc, 8);
  return v.x + v.y;
}

constexpr int kGevThreads = 256;  // 8 warps per block

// Warp-per-output GEMV (SM110a-tuned, modeled on qwen35-thor
// gemv_kernel_scattered):
//   - 8 warps per block, each warp owns one output element
//   - scattered mapping out_idx = blockIdx.x + warp_id * num_blocks
//     (spreads a block's 8 outputs across the grid for DRAM bank locality)
//   - x cooperatively loaded into shared memory ONCE (was: re-read from L2
//     on every K iteration of every thread)
//   - f32x2 SIMD FMA (1.97x scalar FMA throughput on SM110a)
//   - warp-level shuffle reduce (no shared-memory block barrier)
// Deterministic: fixed per-warp reduction order, no atomics.
__global__ void Bf16GevKernel(const uint16_t* __restrict__ w,
                              const uint16_t* __restrict__ x,
                              uint16_t* __restrict__ y, int N, int K,
                              float alpha) {
  constexpr int kWarp = 32;
  constexpr int kWarps = kGevThreads / kWarp;  // 8

  extern __shared__ uint16_t s_x[];

  const int warp_id = threadIdx.x / kWarp;
  const int lane = threadIdx.x & (kWarp - 1);
  const int num_blocks = (N + kWarps - 1) / kWarps;
  const int out_idx = blockIdx.x + warp_id * num_blocks;

  // Cooperative load of x (K bf16) into shared memory, once.
  for (int i = threadIdx.x; i < K; i += kGevThreads)
    s_x[i] = x[i];
  __syncthreads();

  if (out_idx >= N) return;

  const uint16_t* wrow = w + static_cast<size_t>(out_idx) * K;

  // Main loop: 8 bf16 (16B) per iteration, f32x2 FMA.
  uint64_t acc = 0;
  const int k8 = K / 8;
  const float4* w_v4 = reinterpret_cast<const float4*>(wrow);
  const float4* x_v4 = reinterpret_cast<const float4*>(s_x);
  for (int i = lane; i < k8; i += kWarp) {
    const float4 w4 = w_v4[i];
    const float4 x4 = x_v4[i];
    const __nv_bfloat162* w2 = reinterpret_cast<const __nv_bfloat162*>(&w4);
    const __nv_bfloat162* x2 = reinterpret_cast<const __nv_bfloat162*>(&x4);
#pragma unroll
    for (int j = 0; j < 4; ++j) {
      F32x2Fma(acc, __bfloat1622float2(x2[j]), __bfloat1622float2(w2[j]));
    }
  }
  // Tail (K % 8 elements, scalar).
  float sum = F32x2Reduce(acc);
  for (int k = k8 * 8 + lane; k < K; k += kWarp)
    sum += __bfloat162float(__ushort_as_bfloat16(s_x[k])) *
           __bfloat162float(__ushort_as_bfloat16(wrow[k]));

  // Warp reduce (deterministic shfl_down order).
#pragma unroll
  for (int off = kWarp / 2; off > 0; off >>= 1)
    sum += __shfl_down_sync(0xffffffff, sum, off);
  if (lane == 0)
    y[out_idx] = __bfloat16_as_ushort(__float2bfloat16_rn(alpha * sum));
}

// FP8 weight quantization: one block per output row n. Computes the per-row
// absmax, scale = absmax / 448 (e4m3 max), then writes e4m3(w / scale).
// Mirrors the W4A4 absmax convention and the proto (fp8_gemv_proto.cu).
__global__ void QuantizeFp8RowKernel(const uint16_t* __restrict__ w,
                                     uint8_t* __restrict__ w_fp8,
                                     float* __restrict__ scale, int N, int K) {
  const int n = blockIdx.x;
  if (n >= N) return;
  const uint16_t* wrow = w + static_cast<size_t>(n) * K;
  uint8_t* orow = w_fp8 + static_cast<size_t>(n) * K;
  __shared__ float s_amax[kGevThreads];
  float amax = 0.0f;
  for (int k = threadIdx.x; k < K; k += blockDim.x)
    amax = fmaxf(amax, fabsf(__bfloat162float(__ushort_as_bfloat16(wrow[k]))));
  s_amax[threadIdx.x] = amax;
  __syncthreads();
  for (int s = blockDim.x / 2; s > 0; s >>= 1) {
    if (threadIdx.x < s)
      s_amax[threadIdx.x] =
          fmaxf(s_amax[threadIdx.x], s_amax[threadIdx.x + s]);
    __syncthreads();
  }
  const float sc = s_amax[0] > 0.0f ? s_amax[0] / 448.0f : 1.0f;
  if (threadIdx.x == 0) scale[n] = sc;
  const float inv = 1.0f / sc;
  for (int k = threadIdx.x; k < K; k += blockDim.x) {
    const float wv = __bfloat162float(__ushort_as_bfloat16(wrow[k]));
    const __nv_fp8_e4m3 q(wv * inv);
    orow[k] = q.__x;
  }
}

// FP8 W8A16 GEMV (weight e4m3 + per-output-channel scale, activation BF16).
// Mirrors Bf16GevKernel but reads 16 e4m3 (uint4, 16B) per iter and applies
// the row scale once at the end. See tools/fp8_gemv_proto.cu (~2x, DRAM-bound).
__global__ void Fp8GevKernel(const uint8_t* __restrict__ w,
                             const float* __restrict__ wscale,
                             const uint16_t* __restrict__ x,
                             uint16_t* __restrict__ y, int N, int K,
                             float alpha) {
  constexpr int kWarp = 32;
  constexpr int kWarps = kGevThreads / kWarp;  // 8
  extern __shared__ uint16_t s_x[];
  const int warp_id = threadIdx.x / kWarp;
  const int lane = threadIdx.x & (kWarp - 1);
  const int num_blocks = (N + kWarps - 1) / kWarps;
  const int out_idx = blockIdx.x + warp_id * num_blocks;
  for (int i = threadIdx.x; i < K; i += kGevThreads) s_x[i] = x[i];
  __syncthreads();
  if (out_idx >= N) return;
  const uint8_t* wrow = w + static_cast<size_t>(out_idx) * K;
  float sum = 0.0f;
  const int k16 = K / 16;
  const uint4* w_v16 = reinterpret_cast<const uint4*>(wrow);
  for (int i = lane; i < k16; i += kWarp) {
    const uint4 raw = w_v16[i];
    const __nv_fp8_e4m3* wf = reinterpret_cast<const __nv_fp8_e4m3*>(&raw);
    const int base = i * 16;
#pragma unroll
    for (int j = 0; j < 16; ++j)
      sum += __bfloat162float(__ushort_as_bfloat16(s_x[base + j])) *
             static_cast<float>(wf[j]);
  }
  for (int k = k16 * 16 + lane; k < K; k += kWarp) {
    __nv_fp8_e4m3 wv;
    wv.__x = wrow[k];
    sum += __bfloat162float(__ushort_as_bfloat16(s_x[k])) *
           static_cast<float>(wv);
  }
  sum *= wscale[out_idx];
#pragma unroll
  for (int off = kWarp / 2; off > 0; off >>= 1)
    sum += __shfl_down_sync(0xffffffff, sum, off);
  if (lane == 0)
    y[out_idx] = __bfloat16_as_ushort(__float2bfloat16_rn(alpha * sum));
}

}  // namespace

// y[M=1, N] = alpha * sum_k x[k] * W[N, K] (beta must be 0, caller-checked).
// Returns false if the shape is unsupported (caller falls back to GEMM).
bool Bf16Gev(const uint16_t* x, const uint16_t* w, uint16_t* y, int N, int K,
             float alpha, cudaStream_t stream) {
  if (N <= 0 || K <= 0 || (K & 7) != 0) return false;
  // Warp-per-output: 8 warps per block, each warp owns one output element.
  // Grid = ceil(N / 8). Dynamic shared memory holds x (K bf16 = K*2 bytes).
  constexpr int kWarps = kGevThreads / 32;
  const int blocks = (N + kWarps - 1) / kWarps;
  const size_t smem = static_cast<size_t>(K) * sizeof(uint16_t);
  Bf16GevKernel<<<blocks, kGevThreads, smem, stream>>>(w, x, y, N, K, alpha);
  return cudaGetLastError() == cudaSuccess;
}

bool Fp8ProjEnabled(Fp8Part part) {
  const auto on = [](const char* n) {
    const char* v = std::getenv(n);
    return v != nullptr && v[0] != '\0' && !(v[0] == '0' && v[1] == '\0');
  };
  static const bool all = on("Q4T_FP8_PROJ");
  static const bool attn = on("Q4T_FP8_ATTN");
  static const bool gdn = on("Q4T_FP8_GDN");
  static const bool lmhead = on("Q4T_FP8_LMHEAD");
  static const bool shared = on("Q4T_FP8_SHARED");
  if (all) return true;
  switch (part) {
    case Fp8Part::kAttn: return attn;
    case Fp8Part::kGdn: return gdn;
    case Fp8Part::kLmHead: return lmhead;
    case Fp8Part::kMoeShared: return shared;
  }
  return false;
}

void Fp8Shadow::Free() {
  if (w) cudaFree(w);
  if (scale) cudaFree(scale);
  w = nullptr;
  scale = nullptr;
}

bool QuantizeToFp8Shadow(const uint16_t* w_bf16, int N, int K, Fp8Shadow* out,
                         cudaStream_t stream) {
  if (!out) return false;
  out->w = nullptr;
  out->scale = nullptr;
  if (!w_bf16 || N <= 0 || K <= 0 || (K & 15) != 0) return false;
  if (cudaMalloc(&out->w, static_cast<size_t>(N) * K) != cudaSuccess)
    return false;
  if (cudaMalloc(&out->scale, static_cast<size_t>(N) * sizeof(float)) !=
      cudaSuccess) {
    cudaFree(out->w);
    out->w = nullptr;
    return false;
  }
  QuantizeFp8RowKernel<<<N, kGevThreads, 0, stream>>>(w_bf16, out->w,
                                                      out->scale, N, K);
  if (cudaGetLastError() != cudaSuccess) {
    Fp8Shadow tmp{out->w, out->scale};
    tmp.Free();
    out->w = nullptr;
    out->scale = nullptr;
    return false;
  }
  return true;
}

bool BuildFp8Shadow(const uint16_t* w_bf16, int N, int K, Fp8Shadow* out,
                    Fp8Part part, cudaStream_t stream) {
  if (!out) return false;
  out->w = nullptr;
  out->scale = nullptr;
  if (!Fp8ProjEnabled(part)) return true;  // gated off: decode stays BF16
  return QuantizeToFp8Shadow(w_bf16, N, K, out, stream);
}

bool Fp8Gev(const uint16_t* x, const Fp8Shadow& w, uint16_t* y, int N, int K,
            float alpha, cudaStream_t stream) {
  if (!w.w || !w.scale) return false;
  if (N <= 0 || K <= 0 || (K & 15) != 0) return false;
  constexpr int kWarps = kGevThreads / 32;
  const int blocks = (N + kWarps - 1) / kWarps;
  const size_t smem = static_cast<size_t>(K) * sizeof(uint16_t);
  Fp8GevKernel<<<blocks, kGevThreads, smem, stream>>>(w.w, w.scale, x, y, N, K,
                                                       alpha);
  return cudaGetLastError() == cudaSuccess;
}

}  // namespace model
}  // namespace q4t
