// 2 <= M <= 8 BF16 small-M GEMM (y = alpha * x * W^T), SM110a-tuned.
//
// Batched decode / MTP verify (small T) turns projections into tall-skinny
// GEMMs. cuBLASLt is not bandwidth-optimal for these shapes on SM110a
// (measured: tools/small_m_gemm_v2_bench — e.g. HC up N=10240 K=320 M=4:
// cuBLASLt 1024 us vs hand-written 165 us). This file provides the
// hand-written kernels; the shape policy in Bf16SmallMGemm routes only the
// shapes measured to beat cuBLASLt, everything else returns false and the
// caller falls back to the cuBLASLt path in Bf16Gemm.
//
// Two variants (both modeled on the v2 bench kernel, which stages x in
// shared memory once per block and uses f32x2 SIMD FMA, 1.97x scalar FMA
// throughput on SM110a — the same technique as the production Bf16Gev
// decode GEMV):
//   - Scattered: one warp per output column, grid = N/16 blocks. Best for
//     large-N / moderate-K shapes (in_proj_qkv/z, QSA qg, HC up/inject).
//   - KSplit: one block per output column, 16 warps split K with a shared
//     cross-warp reduce. For small-N / large-K shapes (grid = N blocks).
//
// Deterministic: fixed per-warp / per-block reduction order, no atomics, so
// repeated steps are bit-stable (same contract as Bf16Gev).
#include <cuda_bf16.h>
#include <cuda_runtime.h>

#include <cstdint>
#include <cstring>

#include "q4t/model/gemv.h"

namespace q4t {
namespace model {
namespace {

// f32x2 SIMD FMA: 2 FMAs per instruction on SM110a (1.97x scalar
// throughput). Accumulates {a.x*b.x, a.y*b.y} into the packed 64-bit pair.
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

constexpr int kThreads = 512;  // 16 warps per block
constexpr int kWarp = 32;
constexpr int kWarps = kThreads / kWarp;

// y[m, n] = alpha * sum_k W[n, k] * x[m, k]. W [N, K] row-major bf16,
// x [M, K] row-major bf16, y [M, N] row-major bf16. One warp per output
// column n (scattered mapping); x staged in smem once per block.
template <int MAXM>
__global__ void Bf16SmallMScatteredKernel(
    const uint16_t* __restrict__ w, const uint16_t* __restrict__ x,
    uint16_t* __restrict__ y, int M, int N, int K, float alpha) {
  extern __shared__ __align__(16) uint16_t s_x[];  // [MAXM, K]

  const int lane = threadIdx.x & (kWarp - 1);
  const int warp_id = threadIdx.x / kWarp;
  const int num_blocks = (N + kWarps - 1) / kWarps;
  const int n = blockIdx.x + warp_id * num_blocks;

  // Cooperative x load (8 bf16 per float4).
  const int total_v4 = M * K / 8;
  auto* sv = reinterpret_cast<float4*>(s_x);
  const auto* xv = reinterpret_cast<const float4*>(x);
  for (int i = threadIdx.x; i < total_v4; i += kThreads) sv[i] = xv[i];
  __syncthreads();

  if (n >= N) return;
  const float4* w8 =
      reinterpret_cast<const float4*>(w + static_cast<size_t>(n) * K);
  const int k8 = K / 8;
  const int xk8 = K / 8;

  uint64_t acc[MAXM];
#pragma unroll
  for (int m = 0; m < MAXM; ++m) acc[m] = 0;

  for (int i = lane; i < k8; i += kWarp) {
    const float4 wr = w8[i];  // 8 bf16 weights
    const __nv_bfloat162* w2 = reinterpret_cast<const __nv_bfloat162*>(&wr);
    float2 wf[4];
#pragma unroll
    for (int j = 0; j < 4; ++j) wf[j] = __bfloat1622float2(w2[j]);
#pragma unroll
    for (int m = 0; m < MAXM; ++m) {
      const float4 xr = sv[m * xk8 + i];
      const __nv_bfloat162* x2 = reinterpret_cast<const __nv_bfloat162*>(&xr);
#pragma unroll
      for (int j = 0; j < 4; ++j) {
        const float2 xf = __bfloat1622float2(x2[j]);
        F32x2Fma(acc[m], wf[j], xf);
      }
    }
  }
#pragma unroll
  for (int m = 0; m < MAXM; ++m) {
    float s = F32x2Reduce(acc[m]);
#pragma unroll
    for (int off = 16; off > 0; off >>= 1)
      s += __shfl_down_sync(0xffffffff, s, off);
    if (lane == 0 && m < M)
      y[static_cast<size_t>(m) * N + n] =
          __bfloat16_as_ushort(__float2bfloat16_rn(s * alpha));
  }
}

// K-split variant for small-N / large-K shapes: one block per output column,
// 16 warps split K, cross-warp reduce through shared memory.
template <int MAXM>
__global__ void Bf16SmallMKSplitKernel(const uint16_t* __restrict__ w,
                                       const uint16_t* __restrict__ x,
                                       uint16_t* __restrict__ y, int M, int N,
                                       int K, float alpha) {
  extern __shared__ __align__(16) uint16_t s_x[];  // [MAXM, K]
  const int n = blockIdx.x;
  if (n >= N) return;

  const int total_v4 = M * K / 8;
  auto* sv = reinterpret_cast<float4*>(s_x);
  const auto* xv = reinterpret_cast<const float4*>(x);
  for (int i = threadIdx.x; i < total_v4; i += kThreads) sv[i] = xv[i];
  __syncthreads();

  const int lane = threadIdx.x & (kWarp - 1);
  const int warp_id = threadIdx.x / kWarp;
  const int k8 = K / 8;
  const int xk8 = K / 8;
  // Each warp covers a contiguous chunk of the k8 iterations.
  const int per_warp = (k8 + kWarps - 1) / kWarps;
  const int i0 = warp_id * per_warp;
  const int i1 = std::min(k8, i0 + per_warp);
  const float4* w8 =
      reinterpret_cast<const float4*>(w + static_cast<size_t>(n) * K);

  uint64_t acc[MAXM];
#pragma unroll
  for (int m = 0; m < MAXM; ++m) acc[m] = 0;
  for (int i = i0 + lane; i < i1; i += kWarp) {
    const float4 wr = w8[i];
    const __nv_bfloat162* w2 = reinterpret_cast<const __nv_bfloat162*>(&wr);
    float2 wf[4];
#pragma unroll
    for (int j = 0; j < 4; ++j) wf[j] = __bfloat1622float2(w2[j]);
#pragma unroll
    for (int m = 0; m < MAXM; ++m) {
      const float4 xr = sv[m * xk8 + i];
      const __nv_bfloat162* x2 = reinterpret_cast<const __nv_bfloat162*>(&xr);
#pragma unroll
      for (int j = 0; j < 4; ++j) {
        const float2 xf = __bfloat1622float2(x2[j]);
        F32x2Fma(acc[m], wf[j], xf);
      }
    }
  }
  // Cross-warp reduce through smem (reuse s_x after the K loop; x is done).
  float* s_red = reinterpret_cast<float*>(s_x);  // [kWarps, MAXM]
#pragma unroll
  for (int m = 0; m < MAXM; ++m) {
    float s = F32x2Reduce(acc[m]);
#pragma unroll
    for (int off = 16; off > 0; off >>= 1)
      s += __shfl_down_sync(0xffffffff, s, off);
    if (lane == 0) s_red[warp_id * MAXM + m] = s;
  }
  __syncthreads();
  if (threadIdx.x < MAXM) {
    const int m = threadIdx.x;
    float s = 0.0f;
    for (int wq = 0; wq < kWarps; ++wq) s += s_red[wq * MAXM + m];
    if (m < M)
      y[static_cast<size_t>(m) * N + n] =
          __bfloat16_as_ushort(__float2bfloat16_rn(s * alpha));
  }
}

template <int MAXM>
bool LaunchScattered(const uint16_t* w, const uint16_t* x, uint16_t* y, int M,
                     int N, int K, float alpha, cudaStream_t stream) {
  // Stage MAXM rows so the unrolled m<MAXM compute never reads past the
  // dynamic smem when M<MAXM (rows M..MAXM-1 are computed but not stored).
  const size_t smem = static_cast<size_t>(MAXM) * K * sizeof(uint16_t);
  const int blocks = (N + kWarps - 1) / kWarps;
  auto kfn = Bf16SmallMScatteredKernel<MAXM>;
  if (cudaFuncSetAttribute(kfn, cudaFuncAttributeMaxDynamicSharedMemorySize,
                           static_cast<int>(smem)) != cudaSuccess)
    return false;
  kfn<<<blocks, kThreads, smem, stream>>>(w, x, y, M, N, K, alpha);
  return cudaGetLastError() == cudaSuccess;
}

template <int MAXM>
bool LaunchKSplit(const uint16_t* w, const uint16_t* x, uint16_t* y, int M,
                  int N, int K, float alpha, cudaStream_t stream) {
  const size_t smem = static_cast<size_t>(MAXM) * K * sizeof(uint16_t);
  auto kfn = Bf16SmallMKSplitKernel<MAXM>;
  if (cudaFuncSetAttribute(kfn, cudaFuncAttributeMaxDynamicSharedMemorySize,
                           static_cast<int>(smem)) != cudaSuccess)
    return false;
  kfn<<<N, kThreads, smem, stream>>>(w, x, y, M, N, K, alpha);
  return cudaGetLastError() == cudaSuccess;
}

}  // namespace

// Shape policy: route 2 <= M <= 8 dense GEMMs (K % 8 == 0, N >= 320) to the
// hand-written small-M kernel; small-N (N < 320) falls back to cuBLASLt.
// Rationale (MTP correctness, 2026-10-12; small-N carve-out 2026-10-13):
// the MTP verify step (T = k+1, e.g. T=4) must compute per-token logits
// bit-identical to the sequential decode (T=1) path, or the speculative
// accept/reject can flip a near-tie argmax and the MTP output diverges
// from plain greedy (observed: 1K smoke, first diff at char 270). The T=1
// path uses Bf16Gev (GEMV) for every dense projection; the Scattered
// small-M kernel reproduces Bf16GevKernel's per-lane K accumulation order
// exactly, so routing every dense shape keeps the whole T-path GEMV-aligned.
//
// Performance (tools/small_m_gemm_v2_bench, DRAM regime): at M=4 (the verify
// T=4 case) every dense projection shape is a cuBLASLt tie or win
// (0.98-1.11x); the only clear loser is the rare chunked-prefill last chunk
// (M=8) on a few shapes (0.85-0.93x), which never touches the decode/verify
// hot path. cuBLASLt is not the fastest small-M path on SM110a anyway.
//
// All main-model T-dependent dense projections have K % 8 == 0:
//   in_proj_qkv/in_proj_z/qg/HC inject/MoE router/PLE key (K=2560),
//   out_proj/o_proj (K=6144), HC up (K=320), HC down (K=10240),
//   PLE value (K=2560), lm_head (K=2560).
bool Bf16SmallMShapeRouted(int M, int N, int K) {
  if (M < 2 || M > 8 || K % 8 != 0) return false;
  // Small-N carve-out (N < 320): the Scattered kernel launches ceil(N/16)
  // blocks, so very small N (HC inject N=4, in_proj_a/b N=48) would use 1-3
  // blocks and starve the 20 SMs; those fall back to cuBLASLt.
  //
  // N=320 (HC down, K=10240) IS routed to Scattered. The earlier carve-out
  // (N<512) was based on tools/small_m_gemm_v2_bench, which for N<512 shapes
  // measured the KSplit variant (not Scattered) and reported a cuBLASLt time
  // (42 us) that does not reproduce in production or in
  // tools/small_m_gemm_v3_bench. Direct evidence (2026-10-13, Thor 20 SMs /
  // 32 MB L2): production sub-timing measures the cuBLASLt HC-down at
  // ~146 us/call (14.0 ms / 96 calls per verify forward); the v3 bench
  // (SCAT512 == production Bf16SmallMScatteredKernel, L2-defeated) measures
  // Scattered 59.5 us vs cuBLASLt 157 us on the same shape. So Scattered is
  // ~2.6x faster here AND restores the bit-identical-to-GEMV accumulation the
  // T-path MTP contract prefers (the mix-down GEMM no longer carries the
  // cuBLASLt-vs-GEMV 1-2 ULP delta). Re-validated by mtp_verify_numerics.
  if (N < 320) return false;
  return true;
}

bool Bf16SmallMGemm(const uint16_t* x, const uint16_t* w, uint16_t* y, int M,
                    int N, int K, float alpha, cudaStream_t stream) {
  if (x == nullptr || w == nullptr || y == nullptr) return false;
  if (!Bf16SmallMShapeRouted(M, N, K)) return false;
  // Scattered variant for every routed shape: it is bit-identical to
  // Bf16Gev per row for all MAXM (the T-path GEMV-alignment contract), and
  // the v2 bench shows it ties the KSplit variant on the small-N / large-K
  // HC-down shape (N=320, K=10240) while winning on the large-N shapes.
  // One warp per output column; grid = ceil(N/16) blocks.
  if (M <= 4) return LaunchScattered<4>(w, x, y, M, N, K, alpha, stream);
  return LaunchScattered<8>(w, x, y, M, N, K, alpha, stream);
}

}  // namespace model
}  // namespace q4t
