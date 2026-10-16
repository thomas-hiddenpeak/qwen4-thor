// Small-M GEMM v2 bench: SM110a-tuned hand-written BF16 kernel vs cuBLASLt.
//
// v1 (small_m_gemm_bench.cu) re-read x from L2 on every K iteration of every
// column warp, which degrades at M>=4 (x L2 traffic = N*M*K*2 bytes). v2:
//   - x cooperatively loaded into shared memory ONCE per block (bf16)
//   - f32x2 SIMD FMA (1.97x scalar FMA throughput on SM110a, same as the
//     production Bf16Gev decode GEMV)
//   - 16 warps/block, one warp per output column, scattered mapping
//   - MAXM templated (4 / 8) so no per-m runtime branch in the hot loop
//
// Shapes = the model's real dense projections (docs/MODEL.md). L2-defeated
// weight pool (pool > L2) so this is the DRAM regime real decode sees.
//
// Build: q4t_small_m_gemm_v2_bench
#include <cublasLt.h>
#include <cuda_bf16.h>
#include <cuda_runtime.h>

#include <algorithm>
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <functional>
#include <random>
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

// f32x2 SIMD FMA: 2 FMAs per instruction on SM110a.
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

// y[m, n] = alpha * sum_k W[n, k] * x[m, k].  W [N, K] row-major bf16,
// x [M, K] row-major bf16, y [M, N] row-major bf16.
// One warp per output column n (scattered). x staged in smem once per block.
template <int MAXM>
__global__ void Bf16SmallMKernelV2(const uint16_t* __restrict__ w,
                                   const uint16_t* __restrict__ x,
                                   uint16_t* __restrict__ y, int M, int N,
                                   int K, float alpha) {
  constexpr int kWarp = 32;
  constexpr int kWarps = kThreads / kWarp;  // 16
  extern __shared__ __align__(16) uint16_t s_x[];  // [M, K]

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
          __bfloat16_as_ushort(__float2bfloat16(s * alpha));
  }
}

// K-split variant for small-N / large-K shapes (e.g. HC down N=320 K=10240):
// one block per column, 16 warps split K, smem cross-warp reduction.
template <int MAXM>
__global__ void Bf16SmallMKernelKSplit(
    const uint16_t* __restrict__ w, const uint16_t* __restrict__ x,
    uint16_t* __restrict__ y, int M, int N, int K, float alpha) {
  constexpr int kWarp = 32;
  constexpr int kWarps = kThreads / kWarp;  // 16
  extern __shared__ __align__(16) uint16_t s_x[];  // [M, K]
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
          __bfloat16_as_ushort(__float2bfloat16(s * alpha));
  }
}

template <int MAXM>
void LaunchV2(const uint16_t* w, const uint16_t* x, uint16_t* y, int M, int N,
              int K, float alpha, cudaStream_t stream, bool ksplit) {
  // Stage MAXM rows so the unrolled m<MAXM compute never reads past the
  // dynamic smem when M<MAXM (rows M..MAXM-1 are computed but not stored).
  const size_t smem = static_cast<size_t>(MAXM) * K * sizeof(uint16_t);
  if (ksplit) {
    const int blocks = N;
    auto kfn = Bf16SmallMKernelKSplit<MAXM>;
    if (cudaFuncSetAttribute(kfn, cudaFuncAttributeMaxDynamicSharedMemorySize,
                             static_cast<int>(smem)) != cudaSuccess) {
      std::printf("  (KSplit: cudaFuncSetAttribute failed, smem=%zu)\n", smem);
      std::exit(1);
    }
    kfn<<<blocks, kThreads, smem, stream>>>(w, x, y, M, N, K, alpha);
  } else {
    const int blocks = (N + kThreads / 32 - 1) / (kThreads / 32);
    auto kfn = Bf16SmallMKernelV2<MAXM>;
    if (cudaFuncSetAttribute(kfn, cudaFuncAttributeMaxDynamicSharedMemorySize,
                             static_cast<int>(smem)) != cudaSuccess) {
      std::printf("  (V2: cudaFuncSetAttribute failed, smem=%zu)\n", smem);
      std::exit(1);
    }
    kfn<<<blocks, kThreads, smem, stream>>>(w, x, y, M, N, K, alpha);
  }
}

void LaunchV2(const uint16_t* w, const uint16_t* x, uint16_t* y, int M, int N,
              int K, float alpha, cudaStream_t stream, bool ksplit) {
  if (M <= 4)
    LaunchV2<4>(w, x, y, M, N, K, alpha, stream, ksplit);
  else
    LaunchV2<8>(w, x, y, M, N, K, alpha, stream, ksplit);
}

double L2Rel(const std::vector<float>& a, const std::vector<float>& ref) {
  double num = 0, den = 0;
  for (size_t i = 0; i < a.size(); ++i) {
    const double d = a[i] - ref[i];
    num += d * d;
    den += static_cast<double>(ref[i]) * ref[i];
  }
  return std::sqrt(num / (den + 1e-30));
}

struct Shape {
  const char* name;
  int N, K;
  int layers;  // how many times this shape appears per forward (weight reads)
};

const Shape kShapes[] = {
    {"GDN in_proj_qkv (36L)", 10240, 2560, 36},
    {"GDN in_proj_z (36L)", 6144, 2560, 36},
    {"GDN out_proj (36L)", 2560, 6144, 36},
    {"QSA qg (12L)", 7168, 2560, 12},
    {"QSA o_proj (12L)", 2560, 6144, 12},
    {"HC down (48x2)", 320, 10240, 96},
    {"HC up (48x2)", 10240, 320, 96},
    {"HC inject (48x2)", 10240, 2560, 96},
    {"lm_head (1)", 248320, 2560, 1},
};

cublasLtHandle_t g_lt = nullptr;
void* g_lt_ws = nullptr;
uint16_t* g_l2killer = nullptr;
size_t g_l2killer_bytes = 0;
constexpr size_t kLtWsBytes = 32u << 20;

float RunCublas(const uint16_t* x, const uint16_t* w, uint16_t* y, int M,
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
    std::printf("  cublasLt heuristic failed (status=%d nres=%d) M=%d N=%d K=%d\n",
                (int)hs, nres, M, N, K);
    cublasLtMatrixLayoutDestroy(la);
    cublasLtMatrixLayoutDestroy(lb);
    cublasLtMatrixLayoutDestroy(lc);
    cublasLtMatmulDescDestroy(op);
    return -1.0f;
  }
  const float alpha = 1.0f, beta = 0.0f;
  cudaEvent_t e0, e1;
  CK(cudaEventCreate(&e0));
  CK(cudaEventCreate(&e1));
  constexpr int kIters = 20;
  cublasStatus_t s0 = cublasLtMatmul(
      g_lt, op, &alpha, w, la, x, lb,
      &beta, y, lc, y, lc, &heur.algo, g_lt_ws, kLtWsBytes, stream);
  if (s0 != CUBLAS_STATUS_SUCCESS) {
    std::printf("  cublasLtMatmul failed (status=%d) M=%d N=%d K=%d\n",
                (int)s0, M, N, K);
    cudaEventDestroy(e0);
    cudaEventDestroy(e1);
    cublasLtMatrixLayoutDestroy(la);
    cublasLtMatrixLayoutDestroy(lb);
    cublasLtMatrixLayoutDestroy(lc);
    cublasLtMatmulDescDestroy(op);
    return -1.0f;
  }
  float total_ms = 0;
  for (int it = 0; it < kIters; ++it) {
    if (g_l2killer) {
      CK(cudaMemsetAsync(g_l2killer, 0, g_l2killer_bytes * sizeof(uint16_t), stream));
      CK(cudaStreamSynchronize(stream));
    }
    CK(cudaEventRecord(e0, stream));
    cublasStatus_t s = cublasLtMatmul(
        g_lt, op, &alpha, w, la, x, lb,
        &beta, y, lc, y, lc, &heur.algo, g_lt_ws, kLtWsBytes, stream);
    if (s != CUBLAS_STATUS_SUCCESS) {
      std::printf("  cublasLtMatmul iter %d failed (status=%d)\n", it, (int)s);
      break;
    }
    CK(cudaEventRecord(e1, stream));
    CK(cudaEventSynchronize(e1));
    float ms;
    CK(cudaEventElapsedTime(&ms, e0, e1));
    total_ms += ms;
  }
  cudaEventDestroy(e0);
  cudaEventDestroy(e1);
  cublasLtMatrixLayoutDestroy(la);
  cublasLtMatrixLayoutDestroy(lb);
  cublasLtMatrixLayoutDestroy(lc);
  cublasLtMatmulDescDestroy(op);
  return total_ms / kIters;
}

float TimeKernel(std::function<void()> fn, cudaStream_t stream, int iters) {
  cudaEvent_t e0, e1;
  CK(cudaEventCreate(&e0));
  CK(cudaEventCreate(&e1));
  fn();  // warmup
  float total_ms = 0;
  for (int it = 0; it < iters; ++it) {
    if (g_l2killer) {
      CK(cudaMemsetAsync(g_l2killer, 0, g_l2killer_bytes * sizeof(uint16_t), stream));
      CK(cudaStreamSynchronize(stream));
    }
    CK(cudaEventRecord(e0, stream));
    fn();
    CK(cudaEventRecord(e1, stream));
    CK(cudaEventSynchronize(e1));
    float ms;
    CK(cudaEventElapsedTime(&ms, e0, e1));
    total_ms += ms;
  }
  cudaEventDestroy(e0);
  cudaEventDestroy(e1);
  return total_ms / iters;
}

}  // namespace

int main() {
  CK(cudaSetDevice(0));
  cudaDeviceProp prop;
  CK(cudaGetDeviceProperties(&prop, 0));
  std::printf("Small-M GEMM v2 bench (Thor %s, %d SMs, L2 %.1f MB)\n",
              prop.name, prop.multiProcessorCount,
              static_cast<double>(prop.l2CacheSize) / (1024 * 1024));
  cublasLtCreate(&g_lt);
  CK(cudaMalloc(&g_lt_ws, kLtWsBytes));
  // L2 killer: a buffer larger than L2 (32 MB) that we memset before every
  // timed iteration so the weight reads hit DRAM even for shapes smaller than
  // L2 (HC up/down 6.55 MB, in_proj_z/out_proj/o_proj 31.5 MB). Without this
  // those shapes time L2-cached and the cuBLASLt numbers are too optimistic.
  g_l2killer_bytes = 64u << 20;
  CK(cudaMalloc(&g_l2killer, g_l2killer_bytes * sizeof(uint16_t)));

  const int kMaxM = 8;
  const int kMaxN = 248320;
  const int kMaxK = 10240;
  // L2-defeated weight pool: every (shape, M) pair gets its own weight slice
  // so repeated timing iterations stream fresh DRAM data.
  const int kMs[] = {2, 4, 5, 8};
  size_t pool_elems = 0;
  for (const Shape& s : kShapes)
    for (int m : kMs) pool_elems += static_cast<size_t>(s.N) * s.K;
  std::printf("weight pool: %.1f GB\n",
              static_cast<double>(pool_elems) * 2 / 1e9);
  uint16_t* d_pool = nullptr;
  CK(cudaMalloc(&d_pool, pool_elems * sizeof(uint16_t)));
  std::mt19937 rng(1234);
  std::normal_distribution<float> nd(0.0f, 0.02f);
  std::vector<uint16_t> h_pool(pool_elems);
  for (size_t i = 0; i < pool_elems; ++i)
    h_pool[i] = __bfloat16_as_ushort(__float2bfloat16(nd(rng)));
  CK(cudaMemcpy(d_pool, h_pool.data(), pool_elems * sizeof(uint16_t),
                cudaMemcpyHostToDevice));

  uint16_t* d_x = nullptr;
  uint16_t* d_y = nullptr;
  CK(cudaMalloc(&d_x, static_cast<size_t>(kMaxM) * kMaxK * sizeof(uint16_t)));
  CK(cudaMalloc(&d_y, static_cast<size_t>(kMaxM) * kMaxN * sizeof(uint16_t)));
  std::vector<uint16_t> h_x(kMaxM * kMaxK);
  for (auto& v : h_x) v = __bfloat16_as_ushort(__float2bfloat16(nd(rng)));
  CK(cudaMemcpy(d_x, h_x.data(), h_x.size() * sizeof(uint16_t),
                cudaMemcpyHostToDevice));

  cudaStream_t stream;
  CK(cudaStreamCreate(&stream));

  // Correctness check: v2 vs cuBLASLt on a per-shape FP32 reference is too
  // heavy for lm_head; instead check v2 against cuBLASLt output (both FP32
  // accumulate; expect only ULP-level differences) on the first 4096 outputs.
  std::printf(
      "\n== correctness (v2 vs cuBLASLt, l2rel over first 4096 out elems) ==\n");
  size_t off = 0;
  for (const Shape& s : kShapes) {
    const uint16_t* w = d_pool + off;
    off += static_cast<size_t>(s.N) * s.K;
    for (int m : kMs) {
      const bool ksplit = s.N < 512;
      LaunchV2(w, d_x, d_y, m, s.N, s.K, 1.0f, stream, ksplit);
      CK(cudaGetLastError());
      float t_cub = RunCublas(d_x, w, d_y, m, s.N, s.K, stream);
      // recompute v2 into a second buffer for comparison
      uint16_t* d_y2 = nullptr;
      CK(cudaMalloc(&d_y2, static_cast<size_t>(m) * s.N * sizeof(uint16_t)));
      LaunchV2(w, d_x, d_y2, m, s.N, s.K, 1.0f, stream, ksplit);
      CK(cudaGetLastError());
      const int chk = std::min(s.N, 4096);
      std::vector<uint16_t> a(m * chk), b(m * chk);
      CK(cudaMemcpy(a.data(), d_y, m * chk * sizeof(uint16_t),
                    cudaMemcpyDeviceToHost));
      CK(cudaMemcpy(b.data(), d_y2, m * chk * sizeof(uint16_t),
                    cudaMemcpyDeviceToHost));
      std::vector<float> fa(a.size()), fb(b.size());
      for (size_t i = 0; i < a.size(); ++i) {
        fa[i] = __bfloat162float(__ushort_as_bfloat16(a[i]));
        fb[i] = __bfloat162float(__ushort_as_bfloat16(b[i]));
      }
      double l2 = L2Rel(fa, fb);
      std::printf("  %-22s M=%d l2rel=%.2e %s\n", s.name, m, l2,
                  l2 < 1e-2 ? "OK" : "CHECK");
      CK(cudaFree(d_y2));
      (void)t_cub;
    }
  }

  std::printf(
      "\n== timing (us, GB/s effective; cuBLASLt vs v2 vs v2-ksplit) ==\n");
  for (const Shape& s : kShapes) {
    const size_t wbytes = static_cast<size_t>(s.N) * s.K * 2;
    std::printf("\n== %s  N=%d K=%d (%.1f MB) ==\n", s.name, s.N, s.K,
                wbytes / 1e6);
    std::printf(
        "  M |  cuBLASLt GB/s |   v2 GB/s |  v2/cub |  v2ks GB/s |  v2ks/cub\n");
    size_t toff = 0;
    for (int m : kMs) {
      const uint16_t* w = d_pool + toff;  // per-M slice: DRAM regime even for
      toff += static_cast<size_t>(s.N) * s.K;  // shapes smaller than L2
      float t_cub = RunCublas(d_x, w, d_y, m, s.N, s.K, stream);
      auto fn_v2 = [&]() {
        LaunchV2(w, d_x, d_y, m, s.N, s.K, 1.0f, stream, s.N < 512);
      };
      float t_v2 = TimeKernel(fn_v2, stream, 20);
      float t_v2k =
          s.N < 512 ? t_v2 : TimeKernel([&]() { LaunchV2(w, d_x, d_y, m, s.N,
                                                         s.K, 1.0f, stream,
                                                         true); },
                                        stream, 20);
      auto gbs = [&](float t) {
        return t > 0 ? wbytes / (t * 1e-3) / 1e9 : 0.0;
      };
      if (t_cub < 0) {
        std::printf("  %2d |   FAILED  -    | %7.1f %6.0f |  n/a  | %7.1f %6.0f |  n/a \n",
                    m, t_v2 * 1000, gbs(t_v2), t_v2k * 1000, gbs(t_v2k));
      } else {
        std::printf(
            "  %2d | %7.1f %6.0f | %7.1f %6.0f | %5.2fx | %7.1f %6.0f | %5.2fx\n",
            m, t_cub * 1000, gbs(t_cub), t_v2 * 1000, gbs(t_v2),
            t_cub / t_v2, t_v2k * 1000, gbs(t_v2k), t_cub / t_v2k);
      }
    }
  }
  std::printf("\nDone.\n");
  return 0;
}
