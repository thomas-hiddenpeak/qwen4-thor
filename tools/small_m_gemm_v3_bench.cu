// Small-M GEMM v3 bench: contiguous-mapping variants vs scattered (current
// production) vs cuBLASLt, on the real verify (T=4) shapes.
//
// v3 hypotheses (from nsys/sub-timing, 2026-10-13): the production Scattered
// kernel maps the 16 warps of a block to W rows 640 columns apart
// (n = blockIdx.x + warp_id*num_blocks), so each block streams 16 x 640B
// chunks scattered across the weight matrix -> DRAM row thrash. Contiguous
// mapping (block covers 16 adjacent columns) streams ~10KB per block.
// Contiguous is BIT-IDENTICAL to scattered (per-column accumulation order
// unchanged; only the column-to-warp assignment moves).
//
// Variants:
//   SCAT512  : production scattered, 512 threads (16 warps)
//   CONT512  : contiguous, 512 threads
//   CONT256  : contiguous, 256 threads (8 warps, ref-project style)
//   CONT128  : contiguous, 128 threads (4 warps)
//   KSPLIT512: one block per column, 16 warps split K (small-N/large-K)
//   UNROLL512: contiguous + compile-time K8 full unroll (small K)
//   CUBLASLT : cuBLASLt reference
//
// Build: q4t_small_m_gemm_v3_bench
#include <cublasLt.h>
#include <cuda_bf16.h>
#include <cuda_runtime.h>

#include <algorithm>
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <vector>

namespace {

#define CK(x)                                                     \
  do {                                                            \
    cudaError_t e = (x);                                          \
    if (e != cudaSuccess) {                                       \
      std::printf("CUDA error %s:%d: %s\n", __FILE__, __LINE__,   \
                  cudaGetErrorString(e));                         \
      std::exit(1);                                               \
    }                                                             \
  } while (0)

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

constexpr int kWarp = 32;

template <int MAXM>
__device__ __forceinline__ void ColumnCore(
    const uint16_t* __restrict__ w, const uint16_t* __restrict__ s_x,
    uint16_t* __restrict__ y, int M, int N, int K, float alpha, int n,
    int lane) {
  const float4* w8 =
      reinterpret_cast<const float4*>(w + static_cast<size_t>(n) * K);
  const int k8 = K / 8;
  const int xk8 = K / 8;
  uint64_t acc[MAXM];
#pragma unroll
  for (int m = 0; m < MAXM; ++m) acc[m] = 0;
  for (int i = lane; i < k8; i += kWarp) {
    const float4 wr = w8[i];
    const __nv_bfloat162* w2 = reinterpret_cast<const __nv_bfloat162*>(&wr);
    float2 wf[4];
#pragma unroll
    for (int j = 0; j < 4; ++j) wf[j] = __bfloat1622float2(w2[j]);
#pragma unroll
    for (int m = 0; m < MAXM; ++m) {
      const float4 xr =
          reinterpret_cast<const float4*>(s_x)[m * xk8 + i];
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

// Scattered (production): warp w of block b -> column b + w*ceil(N/warps).
template <int MAXM, int T>
__global__ void KScat(const uint16_t* __restrict__ w,
                      const uint16_t* __restrict__ x,
                      uint16_t* __restrict__ y, int M, int N, int K,
                      float alpha) {
  constexpr int kWarps = T / kWarp;
  extern __shared__ __align__(16) uint16_t s_x[];
  const int lane = threadIdx.x & (kWarp - 1);
  const int warp_id = threadIdx.x / kWarp;
  const int num_blocks = (N + kWarps - 1) / kWarps;
  const int n = blockIdx.x + warp_id * num_blocks;
  const int total_v4 = M * K / 8;
  auto* sv = reinterpret_cast<float4*>(s_x);
  const auto* xv = reinterpret_cast<const float4*>(x);
  for (int i = threadIdx.x; i < total_v4; i += T) sv[i] = xv[i];
  __syncthreads();
  if (n >= N) return;
  ColumnCore<MAXM>(w, s_x, y, M, N, K, alpha, n, lane);
}

// Contiguous: warp w of block b -> column b*kWarps + w.
template <int MAXM, int T>
__global__ void KCont(const uint16_t* __restrict__ w,
                      const uint16_t* __restrict__ x,
                      uint16_t* __restrict__ y, int M, int N, int K,
                      float alpha) {
  constexpr int kWarps = T / kWarp;
  extern __shared__ __align__(16) uint16_t s_x[];
  const int lane = threadIdx.x & (kWarp - 1);
  const int warp_id = threadIdx.x / kWarp;
  const int n = blockIdx.x * kWarps + warp_id;
  const int total_v4 = M * K / 8;
  auto* sv = reinterpret_cast<float4*>(s_x);
  const auto* xv = reinterpret_cast<const float4*>(x);
  for (int i = threadIdx.x; i < total_v4; i += T) sv[i] = xv[i];
  __syncthreads();
  if (n >= N) return;
  ColumnCore<MAXM>(w, s_x, y, M, N, K, alpha, n, lane);
}

// KSplit: one block per column, warps split K, smem cross-warp reduce.
template <int MAXM, int T>
__global__ void KKSplit(const uint16_t* __restrict__ w,
                        const uint16_t* __restrict__ x,
                        uint16_t* __restrict__ y, int M, int N, int K,
                        float alpha) {
  constexpr int kWarps = T / kWarp;
  extern __shared__ __align__(16) uint16_t s_x[];
  const int n = blockIdx.x;
  if (n >= N) return;
  const int total_v4 = M * K / 8;
  auto* sv = reinterpret_cast<float4*>(s_x);
  const auto* xv = reinterpret_cast<const float4*>(x);
  for (int i = threadIdx.x; i < total_v4; i += T) sv[i] = xv[i];
  __syncthreads();
  const int lane = threadIdx.x & (kWarp - 1);
  const int warp_id = threadIdx.x / kWarp;
  const int k8 = K / 8;
  const int xk8 = K / 8;
  const int per_warp = (k8 + kWarps - 1) / kWarps;
  const int i0 = warp_id * per_warp;
  const int i1 = (k8 < i0 + per_warp) ? k8 : i0 + per_warp;
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
  float* s_red = reinterpret_cast<float*>(s_x);
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

// Unrolled small-K contiguous: K8 = K/8 template param, fully unrolled so the
// compiler issues all W loads up front (max memory-level parallelism).
template <int MAXM, int K8>
__global__ void KUnroll(const uint16_t* __restrict__ w,
                        const uint16_t* __restrict__ x,
                        uint16_t* __restrict__ y, int M, int N, int K,
                        float alpha) {
  constexpr int T = 512;
  constexpr int kWarps = T / kWarp;
  extern __shared__ __align__(16) uint16_t s_x[];
  const int lane = threadIdx.x & (kWarp - 1);
  const int warp_id = threadIdx.x / kWarp;
  const int n = blockIdx.x * kWarps + warp_id;
  const int total_v4 = M * K / 8;
  auto* sv = reinterpret_cast<float4*>(s_x);
  const auto* xv = reinterpret_cast<const float4*>(x);
  for (int i = threadIdx.x; i < total_v4; i += T) sv[i] = xv[i];
  __syncthreads();
  if (n >= N) return;
  const float4* w8 =
      reinterpret_cast<const float4*>(w + static_cast<size_t>(n) * K);
  const int xk8 = K / 8;
  uint64_t acc[MAXM];
#pragma unroll
  for (int m = 0; m < MAXM; ++m) acc[m] = 0;
#pragma unroll
  for (int r = 0; r < (K8 + kWarp - 1) / kWarp; ++r) {
    const int i = r * kWarp + lane;
    if (i < K8) {
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



// Multi-column per warp: each warp owns C contiguous (in slot order) columns
// and issues ALL C*K8 weight float4 loads up front (fully unrolled) for max
// memory-level parallelism. Targets the large-N / short-K shape (HC up:
// N=10240 K=320) where one-column-per-warp has only ~2 loads in flight.
// Requires N % (C*16) == 0. Bit-diff vs SCAT is expected nonzero (different
// per-column accumulation is unchanged, but this is a NEW mapping; we check
// against a reference GEMM for correctness separately).
template <int MAXM, int C, int K8>
__global__ void KMulti(const uint16_t* __restrict__ w,
                       const uint16_t* __restrict__ x,
                       uint16_t* __restrict__ y, int M, int N, int K,
                       float alpha) {
  constexpr int T = 512;
  constexpr int kWarps = T / kWarp;  // 16
  constexpr int PER_LANE = C * K8 / kWarp;  // float4 per lane
  extern __shared__ __align__(16) uint16_t s_x[];
  const int lane = threadIdx.x & (kWarp - 1);
  const int warp_id = threadIdx.x / kWarp;
  const int slot0 = (blockIdx.x * kWarps + warp_id) * C;
  const int total_v4 = M * K / 8;
  auto* sv = reinterpret_cast<float4*>(s_x);
  const auto* xv = reinterpret_cast<const float4*>(x);
  for (int i = threadIdx.x; i < total_v4; i += T) sv[i] = xv[i];
  __syncthreads();
  // Issue all C columns' weight loads up front.
  float4 wr[PER_LANE];
#pragma unroll
  for (int p = 0; p < PER_LANE; ++p) {
    const int f = p * kWarp + lane;  // 0 .. C*K8-1
    const int c = f / K8;
    const int i = f - c * K8;
    const int n = slot0 + c;
    wr[p] = reinterpret_cast<const float4*>(w + static_cast<size_t>(n) * K)[i];
  }
  uint64_t acc[C][MAXM];
#pragma unroll
  for (int c = 0; c < C; ++c)
#pragma unroll
    for (int m = 0; m < MAXM; ++m) acc[c][m] = 0;
#pragma unroll
  for (int p = 0; p < PER_LANE; ++p) {
    const int f = p * kWarp + lane;
    const int c = f / K8;
    const int i = f - c * K8;
    const __nv_bfloat162* w2 = reinterpret_cast<const __nv_bfloat162*>(&wr[p]);
    float2 wf[4];
#pragma unroll
    for (int j = 0; j < 4; ++j) wf[j] = __bfloat1622float2(w2[j]);
#pragma unroll
    for (int m = 0; m < MAXM; ++m) {
      const float4 xr = sv[m * K8 + i];
      const __nv_bfloat162* x2 = reinterpret_cast<const __nv_bfloat162*>(&xr);
#pragma unroll
      for (int j = 0; j < 4; ++j)
        F32x2Fma(acc[c][m], wf[j], __bfloat1622float2(x2[j]));
    }
  }
#pragma unroll
  for (int c = 0; c < C; ++c) {
    const int n = slot0 + c;
    if (n < N) {
#pragma unroll
      for (int m = 0; m < MAXM; ++m) {
        float s = F32x2Reduce(acc[c][m]);
#pragma unroll
        for (int off = 16; off > 0; off >>= 1)
          s += __shfl_down_sync(0xffffffff, s, off);
        if (lane == 0 && m < M)
          y[static_cast<size_t>(m) * N + n] =
              __bfloat16_as_ushort(__float2bfloat16_rn(s * alpha));
      }
    }
  }
}

template <int C, int K8>
void LaunchMulti(const uint16_t* w, const uint16_t* x, uint16_t* y, int M,
                 int N, int K, float alpha, cudaStream_t s) {
  const size_t smem = static_cast<size_t>(4) * K * 2;
  auto kfn = KMulti<4, C, K8>;
  if (cudaFuncSetAttribute(kfn, cudaFuncAttributeMaxDynamicSharedMemorySize,
                           static_cast<int>(smem)) != cudaSuccess) return;
  const int blocks = N / (C * 16);
  kfn<<<blocks, 512, smem, s>>>(w, x, y, M, N, K, alpha);
}


// REF (reference-project style, qwen35-thor gemv_multirow_kernel_scattered):
// NO shared memory; x rows read from global (L2-hot) inside the K loop.
// Same per-column accumulation as Bf16Gev/Scattered (bit-identical):
// float4 K loop, f32x2 FMA, shfl_down tree, rn(alpha*sum).
template <int MAXM, int T>
__global__ void KRef(const uint16_t* __restrict__ w,
                     const uint16_t* __restrict__ x,
                     uint16_t* __restrict__ y, int M, int N, int K,
                     float alpha) {
  constexpr int kWarps = T / kWarp;
  const int lane = threadIdx.x & (kWarp - 1);
  const int warp_id = threadIdx.x / kWarp;
  const int num_blocks = (N + kWarps - 1) / kWarps;
  const int n = blockIdx.x + warp_id * num_blocks;
  if (n >= N) return;
  const float4* w8 =
      reinterpret_cast<const float4*>(w + static_cast<size_t>(n) * K);
  const int k8 = K / 8;
  uint64_t acc[MAXM];
#pragma unroll
  for (int m = 0; m < MAXM; ++m) acc[m] = 0;
  for (int i = lane; i < k8; i += kWarp) {
    const float4 wr = w8[i];
    const __nv_bfloat162* w2 = reinterpret_cast<const __nv_bfloat162*>(&wr);
    float2 wf[4];
#pragma unroll
    for (int j = 0; j < 4; ++j) wf[j] = __bfloat1622float2(w2[j]);
#pragma unroll
    for (int m = 0; m < MAXM; ++m) {
      if (m < M) {
        const float4 xr =
            reinterpret_cast<const float4*>(x + static_cast<size_t>(m) * K)[i];
        const __nv_bfloat162* x2 = reinterpret_cast<const __nv_bfloat162*>(&xr);
#pragma unroll
        for (int j = 0; j < 4; ++j) {
          const float2 xf = __bfloat1622float2(x2[j]);
          F32x2Fma(acc[m], wf[j], xf);
        }
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

template <int MAXM, int T>
void LaunchScat(const uint16_t* w, const uint16_t* x, uint16_t* y, int M,
                int N, int K, float alpha, cudaStream_t s) {
  const size_t smem = static_cast<size_t>(MAXM) * K * 2;
  auto kfn = KScat<MAXM, T>;
  if (cudaFuncSetAttribute(kfn, cudaFuncAttributeMaxDynamicSharedMemorySize,
                           static_cast<int>(smem)) != cudaSuccess) return;
  kfn<<<(N + T / 32 - 1) / (T / 32), T, smem, s>>>(w, x, y, M, N, K, alpha);
}
template <int MAXM, int T>
void LaunchCont(const uint16_t* w, const uint16_t* x, uint16_t* y, int M,
                int N, int K, float alpha, cudaStream_t s) {
  const size_t smem = static_cast<size_t>(MAXM) * K * 2;
  auto kfn = KCont<MAXM, T>;
  if (cudaFuncSetAttribute(kfn, cudaFuncAttributeMaxDynamicSharedMemorySize,
                           static_cast<int>(smem)) != cudaSuccess) return;
  kfn<<<(N + T / 32 - 1) / (T / 32), T, smem, s>>>(w, x, y, M, N, K, alpha);
}
template <int MAXM, int T>
void LaunchKSplit(const uint16_t* w, const uint16_t* x, uint16_t* y, int M,
                  int N, int K, float alpha, cudaStream_t s) {
  const size_t smem = static_cast<size_t>(MAXM) * K * 2;
  auto kfn = KKSplit<MAXM, T>;
  if (cudaFuncSetAttribute(kfn, cudaFuncAttributeMaxDynamicSharedMemorySize,
                           static_cast<int>(smem)) != cudaSuccess) return;
  kfn<<<N, T, smem, s>>>(w, x, y, M, N, K, alpha);
}

template <int MAXM, int T>
void LaunchRef(const uint16_t* w, const uint16_t* x, uint16_t* y, int M,
               int N, int K, float alpha, cudaStream_t s) {
  auto kfn = KRef<MAXM, T>;
  kfn<<<(N + T / 32 - 1) / (T / 32), T, 0, s>>>(w, x, y, M, N, K, alpha);
}

using Launcher = void (*)(const uint16_t*, const uint16_t*, uint16_t*, int,
                          int, int, float, cudaStream_t);

struct Variant {
  const char* name;
  Launcher fn;
};

struct Shape {
  const char* name;
  int N, K, calls;
};

const Shape kShapes[] = {
    {"HC up     (48x2)", 10240, 320, 96},
    {"HC down   (48x2)", 320, 10240, 96},
    {"in_proj_qkv(36L)", 10240, 2560, 36},
    {"in_proj_z (36L)", 6144, 2560, 36},
    {"out_proj  (36L)", 2560, 6144, 36},
    {"qkv       (12L)", 6656, 2560, 12},
    {"oproj     (12L)", 6144, 2560, 12},
    {"lm_head   (1)", 248320, 2560, 1},
};

cublasLtHandle_t g_lt = nullptr;
void* g_lt_ws = nullptr;
uint16_t* g_l2killer = nullptr;
size_t g_l2killer_elems = 0;
constexpr size_t kLtWsBytes = 32u << 20;

float TimeKernel(Launcher fn, const uint16_t* x, const uint16_t* w,
                 uint16_t* y, int M, int N, int K, cudaStream_t stream) {
  cudaEvent_t e0, e1;
  CK(cudaEventCreate(&e0));
  CK(cudaEventCreate(&e1));
  constexpr int kIters = 30;
  float total = 0;
  for (int it = 0; it < kIters; ++it) {
    if (g_l2killer) {
      CK(cudaMemsetAsync(g_l2killer, 0, g_l2killer_elems * 2, stream));
      CK(cudaStreamSynchronize(stream));
    }
    CK(cudaEventRecord(e0, stream));
    fn(w, x, y, M, N, K, 1.0f, stream);
    CK(cudaEventRecord(e1, stream));
    CK(cudaEventSynchronize(e1));
    float ms = 0;
    CK(cudaEventElapsedTime(&ms, e0, e1));
    if (it > 2) total += ms;
  }
  cudaEventDestroy(e0);
  cudaEventDestroy(e1);
  return total / (kIters - 3);
}

float TimeCublas(const uint16_t* x, const uint16_t* w, uint16_t* y, int M,
                 int N, int K, cudaStream_t stream) {
  cublasLtMatmulDesc_t op;
  cublasLtMatmulDescCreate(&op, CUBLAS_COMPUTE_32F, CUDA_R_32F);
  cublasOperation_t opT = CUBLAS_OP_T, opN = CUBLAS_OP_N;
  cublasLtMatmulDescSetAttribute(op, CUBLASLT_MATMUL_DESC_TRANSA, &opT,
                                 sizeof(opT));
  cublasLtMatmulDescSetAttribute(op, CUBLASLT_MATMUL_DESC_TRANSB, &opN,
                                 sizeof(opN));
  cublasLtMatrixLayout_t la, lb, lc;
  cublasLtMatrixLayoutCreate(&la, CUDA_R_16BF, K, N, K);
  cublasLtMatrixLayoutCreate(&lb, CUDA_R_16BF, K, M, K);
  cublasLtMatrixLayoutCreate(&lc, CUDA_R_16BF, N, M, N);
  cublasLtMatmulPreference_t pref;
  cublasLtMatmulPreferenceCreate(&pref);
  cublasLtMatmulPreferenceSetAttribute(pref,
                                       CUBLASLT_MATMUL_PREF_MAX_WORKSPACE_BYTES,
                                       &kLtWsBytes, sizeof(kLtWsBytes));
  uint32_t mask = CUBLASLT_REDUCTION_SCHEME_NONE;
  cublasLtMatmulPreferenceSetAttribute(pref,
                                       CUBLASLT_MATMUL_PREF_REDUCTION_SCHEME_MASK,
                                       &mask, sizeof(mask));
  cublasLtMatmulHeuristicResult_t heur;
  int nres = 0;
  cublasStatus_t hs =
      cublasLtMatmulAlgoGetHeuristic(g_lt, op, la, lb, lc, lc, pref, 1, &heur,
                                     &nres);
  cublasLtMatmulPreferenceDestroy(pref);
  if (hs != CUBLAS_STATUS_SUCCESS || nres <= 0) {
    cublasLtMatrixLayoutDestroy(la);
    cublasLtMatrixLayoutDestroy(lb);
    cublasLtMatrixLayoutDestroy(lc);
    cublasLtMatmulDescDestroy(op);
    return -1;
  }
  const float alpha = 1.0f, beta = 0.0f;
  cudaEvent_t e0, e1;
  CK(cudaEventCreate(&e0));
  CK(cudaEventCreate(&e1));
  constexpr int kIters = 30;
  float total = 0;
  for (int it = 0; it < kIters; ++it) {
    if (g_l2killer) {
      CK(cudaMemsetAsync(g_l2killer, 0, g_l2killer_elems * 2, stream));
      CK(cudaStreamSynchronize(stream));
    }
    CK(cudaEventRecord(e0, stream));
    cublasStatus_t s = cublasLtMatmul(g_lt, op, &alpha, w, la, x, lb, &beta,
                                      y, lc, y, lc, &heur.algo, g_lt_ws,
                                      kLtWsBytes, stream);
    if (s != CUBLAS_STATUS_SUCCESS) break;
    CK(cudaEventRecord(e1, stream));
    CK(cudaEventSynchronize(e1));
    float ms = 0;
    CK(cudaEventElapsedTime(&ms, e0, e1));
    if (it > 2) total += ms;
  }
  cudaEventDestroy(e0);
  cudaEventDestroy(e1);
  cublasLtMatrixLayoutDestroy(la);
  cublasLtMatrixLayoutDestroy(lb);
  cublasLtMatrixLayoutDestroy(lc);
  cublasLtMatmulDescDestroy(op);
  return total / (kIters - 3);
}

// Bit-exactness: contiguous vs scattered must be identical per element.
int CheckBitEqual(const uint16_t* a, const uint16_t* b, size_t n) {
  std::vector<uint16_t> ha(n), hb(n);
  CK(cudaMemcpy(ha.data(), a, n * 2, cudaMemcpyDeviceToHost));
  CK(cudaMemcpy(hb.data(), b, n * 2, cudaMemcpyDeviceToHost));
  int diff = 0;
  for (size_t i = 0; i < n; ++i)
    if (ha[i] != hb[i]) {
      if (diff < 5)
        std::printf("  bitdiff at %zu: %04x vs %04x\n", i, ha[i], hb[i]);
      diff++;
    }
  return diff;
}

}  // namespace

int main(int argc, char** argv) {
  int M = 4;
  if (argc > 1) M = std::atoi(argv[1]);
  if (cublasLtCreate(&g_lt) != CUBLAS_STATUS_SUCCESS) { std::printf("cublasLtCreate failed\n"); return 1; }
  CK(cudaMalloc(&g_lt_ws, kLtWsBytes));
  // L2 killer: 256 MB pool cycled between iterations.
  g_l2killer_elems = (256u << 20) / 2;
  CK(cudaMalloc(&g_l2killer, g_l2killer_elems * 2));

  cudaStream_t stream;
  CK(cudaStreamCreate(&stream));

  std::printf("M=%d  (us per call | GB/s | x-forward total us)\n", M);
  for (const auto& sh : kShapes) {
    const int N = sh.N, K = sh.K;
    const size_t wbytes = static_cast<size_t>(N) * K * 2;
    uint16_t *d_w, *d_x, *d_y, *d_y2;
    CK(cudaMalloc(&d_w, wbytes));
    CK(cudaMalloc(&d_x, static_cast<size_t>(M) * K * 2));
    CK(cudaMalloc(&d_y, static_cast<size_t>(M) * N * 2));
    CK(cudaMalloc(&d_y2, static_cast<size_t>(M) * N * 2));
    // Random-ish init (deterministic).
    {
      std::vector<uint16_t> hw(N * K);
      for (size_t i = 0; i < hw.size(); ++i)
        hw[i] = static_cast<uint16_t>((i * 2654435761u) & 0x7fff) + 1;
      std::vector<uint16_t> hx(M * K);
      for (size_t i = 0; i < hx.size(); ++i)
        hx[i] = static_cast<uint16_t>((i * 40503u + 7) & 0x7fff);
      CK(cudaMemcpy(d_w, hw.data(), hw.size() * 2, cudaMemcpyHostToDevice));
      CK(cudaMemcpy(d_x, hx.data(), hx.size() * 2, cudaMemcpyHostToDevice));
    }

    struct Res {
      const char* name;
      float us;
    };
    std::vector<Res> res;
    res.push_back({"SCAT512",
                   TimeKernel(LaunchScat<4, 512>, d_x, d_w, d_y, M, N, K,
                              stream) * 1000});
    res.push_back({"CONT512",
                   TimeKernel(LaunchCont<4, 512>, d_x, d_w, d_y, M, N, K,
                              stream) * 1000});
    res.push_back({"CONT256",
                   TimeKernel(LaunchCont<4, 256>, d_x, d_w, d_y, M, N, K,
                              stream) * 1000});
    res.push_back({"CONT128",
                   TimeKernel(LaunchCont<4, 128>, d_x, d_w, d_y, M, N, K,
                              stream) * 1000});
    res.push_back({"KSPLIT512",
                   TimeKernel(LaunchKSplit<4, 512>, d_x, d_w, d_y, M, N, K,
                              stream) * 1000});
    res.push_back({"KSPLIT256",
                   TimeKernel(LaunchKSplit<4, 256>, d_x, d_w, d_y, M, N, K,
                              stream) * 1000});
    // Unrolled variants for small K8.
    if (K / 8 == 40)
      res.push_back({"UNROLL40",
                     TimeKernel([](const uint16_t* w, const uint16_t* x,
                                   uint16_t* y, int m, int n, int k, float a,
                                   cudaStream_t s) {
                       const size_t smem = 4u * k * 2;
                       auto kfn = KUnroll<4, 40>;
                       cudaFuncSetAttribute(
                           kfn, cudaFuncAttributeMaxDynamicSharedMemorySize,
                           static_cast<int>(smem));
                       kfn<<<(n + 15) / 16, 512, smem, s>>>(w, x, y, m, n, k,
                                                            a);
                     },
                     d_x, d_w, d_y, M, N, K, stream) * 1000});
    // Multi-column-per-warp variants (max MLP) for the short-K HC up shape.
    if (K / 8 == 40 && N % 64 == 0)
      res.push_back({"MULTI4",
                     TimeKernel(LaunchMulti<4, 40>, d_x, d_w, d_y, M, N, K,
                                stream) * 1000});
    if (K / 8 == 40 && N % 128 == 0)
      res.push_back({"MULTI8",
                     TimeKernel(LaunchMulti<8, 40>, d_x, d_w, d_y, M, N, K,
                                stream) * 1000});
    res.push_back({"REF8",
                   TimeKernel(LaunchRef<4, 256>, d_x, d_w, d_y, M, N, K,
                              stream) * 1000});
    res.push_back({"REF16",
                   TimeKernel(LaunchRef<4, 512>, d_x, d_w, d_y, M, N, K,
                              stream) * 1000});
    res.push_back({"CUBLASLT",
                   TimeCublas(d_x, d_w, d_y, M, N, K, stream) * 1000});

    std::printf("%-16s W=%7.2fMB  ", sh.name, wbytes / 1e6);
    for (const auto& r : res) {
      const double gbs = wbytes / (r.us * 1e-6) / 1e9;
      std::printf("%s=%7.1fus(%4.0f%%) ", r.name, r.us,
                  gbs / 2.73);
    }
    std::printf("\n");
    // Bit-exactness: CONT512 vs SCAT512.
    LaunchScat<4, 512>(d_w, d_x, d_y, M, N, K, 1.0f, stream);
    LaunchCont<4, 512>(d_w, d_x, d_y2, M, N, K, 1.0f, stream);
    CK(cudaStreamSynchronize(stream));
    const int diff = CheckBitEqual(d_y, d_y2, static_cast<size_t>(M) * N);
    std::printf("  cont512 vs scat512 bitdiff=%d\n", diff);
    LaunchRef<4, 256>(d_w, d_x, d_y2, M, N, K, 1.0f, stream);
    CK(cudaStreamSynchronize(stream));
    const int diff_ref = CheckBitEqual(d_y, d_y2, static_cast<size_t>(M) * N);
    std::printf("  ref8 vs scat512 bitdiff=%d\n", diff_ref);
    CK(cudaFree(d_w));
    CK(cudaFree(d_x));
    CK(cudaFree(d_y));
    CK(cudaFree(d_y2));
  }
  return 0;
}
