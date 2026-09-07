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
#include <cuda_runtime.h>

#include <cstdint>
#include <cstring>

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

}  // namespace model
}  // namespace q4t
