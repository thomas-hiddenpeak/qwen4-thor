// Small-M GEMM bench: does a hand-written W8A16 (e4m3 weight + BF16 act) kernel
// beat the cuBLASLt BF16 GEMM that batched decode (M=B) currently uses, across
// M=1/2/4/8/16 on the real projection shapes?
//
// Rationale: batched decode packs B tokens -> M=B GEMMs. ProjGemm only takes
// the FP8 path at M==1, so M>1 falls back to cuBLASLt BF16 and loses FP8. At
// small M the GEMM is still weight-DRAM-bound (weight bytes >> M*K activation),
// so a kernel that streams the weight ONCE and holds M partial dots in
// registers should stay bandwidth-bound and, with FP8 halving the weight bytes,
// beat the BF16 cuBLASLt path. This measures it.
//
// Curves per (shape, M): cuBLASLt BF16, hand-written BF16 small-M, hand-written
// W8A16 small-M. Synthetic Gaussian weights, L2-defeated (pool > L2) so it is
// the DRAM regime real decode sees. Build: q4t_small_m_gemm_bench.
#include <cublasLt.h>
#include <cuda_bf16.h>
#include <cuda_fp8.h>
#include <cuda_runtime.h>

#include <algorithm>
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <random>
#include <vector>

namespace {

#define CK(x)                                                        \
  do {                                                               \
    cudaError_t e = (x);                                             \
    if (e != cudaSuccess) {                                          \
      std::printf("CUDA error %s:%d: %s\n", __FILE__, __LINE__,      \
                  cudaGetErrorString(e));                            \
      std::exit(1);                                                  \
    }                                                                \
  } while (0)

constexpr int kThreads = 256;  // 8 warps/block

// One warp per output column n: stream W[n,:] once (16 e4m3 / iter via uint4),
// hold M partial dots in registers, read x[m,:] from L2 (small, resident).
template <int MAXM>
__global__ void Fp8SmallMKernel(const uint8_t* __restrict__ w,
                                const float* __restrict__ wscale,
                                const uint16_t* __restrict__ x,
                                uint16_t* __restrict__ y, int M, int N, int K) {
  constexpr int kWarp = 32;
  const int warps = blockDim.x / kWarp;
  const int warp_id = threadIdx.x / kWarp;
  const int lane = threadIdx.x & 31;
  const int num_blocks = (N + warps - 1) / warps;
  const int n = blockIdx.x + warp_id * num_blocks;
  if (n >= N) return;
  const uint4* w16 = reinterpret_cast<const uint4*>(w + static_cast<size_t>(n) * K);
  const int k16 = K / 16;
  float acc[MAXM];
#pragma unroll
  for (int m = 0; m < MAXM; ++m) acc[m] = 0.0f;
  for (int i = lane; i < k16; i += kWarp) {
    const uint4 raw = w16[i];
    const __nv_fp8_e4m3* wf = reinterpret_cast<const __nv_fp8_e4m3*>(&raw);
    float wv[16];
#pragma unroll
    for (int j = 0; j < 16; ++j) wv[j] = static_cast<float>(wf[j]);
    const int base = i * 16;
#pragma unroll
    for (int m = 0; m < MAXM; ++m) {
      if (m >= M) break;
      const float4* xr =
          reinterpret_cast<const float4*>(x + static_cast<size_t>(m) * K + base);
      const float4 xa = xr[0], xb = xr[1];  // 16 bf16
      const __nv_bfloat162* a = reinterpret_cast<const __nv_bfloat162*>(&xa);
      const __nv_bfloat162* b = reinterpret_cast<const __nv_bfloat162*>(&xb);
#pragma unroll
      for (int j = 0; j < 4; ++j) {
        const float2 av = __bfloat1622float2(a[j]);
        acc[m] += av.x * wv[2 * j] + av.y * wv[2 * j + 1];
        const float2 bv = __bfloat1622float2(b[j]);
        acc[m] += bv.x * wv[8 + 2 * j] + bv.y * wv[8 + 2 * j + 1];
      }
    }
  }
  const float sc = wscale[n];
#pragma unroll
  for (int m = 0; m < MAXM; ++m) {
    if (m >= M) break;
    float s = acc[m];
#pragma unroll
    for (int off = 16; off > 0; off >>= 1)
      s += __shfl_down_sync(0xffffffff, s, off);
    if (lane == 0)
      y[static_cast<size_t>(m) * N + n] =
          __bfloat16_as_ushort(__float2bfloat16(s * sc));
  }
}

// Same structure, BF16 weight (no scale): the hand-written BF16 small-M path.
template <int MAXM>
__global__ void Bf16SmallMKernel(const uint16_t* __restrict__ w,
                                 const uint16_t* __restrict__ x,
                                 uint16_t* __restrict__ y, int M, int N, int K) {
  constexpr int kWarp = 32;
  const int warps = blockDim.x / kWarp;
  const int warp_id = threadIdx.x / kWarp;
  const int lane = threadIdx.x & 31;
  const int num_blocks = (N + warps - 1) / warps;
  const int n = blockIdx.x + warp_id * num_blocks;
  if (n >= N) return;
  const float4* w8 = reinterpret_cast<const float4*>(w + static_cast<size_t>(n) * K);
  const int k8 = K / 8;
  float acc[MAXM];
#pragma unroll
  for (int m = 0; m < MAXM; ++m) acc[m] = 0.0f;
  for (int i = lane; i < k8; i += kWarp) {
    const float4 wr = w8[i];  // 8 bf16
    const __nv_bfloat162* w2 = reinterpret_cast<const __nv_bfloat162*>(&wr);
    float wv[8];
#pragma unroll
    for (int j = 0; j < 4; ++j) {
      const float2 wf = __bfloat1622float2(w2[j]);
      wv[2 * j] = wf.x;
      wv[2 * j + 1] = wf.y;
    }
    const int base = i * 8;
#pragma unroll
    for (int m = 0; m < MAXM; ++m) {
      if (m >= M) break;
      const float4 xr =
          *reinterpret_cast<const float4*>(x + static_cast<size_t>(m) * K + base);
      const __nv_bfloat162* xa = reinterpret_cast<const __nv_bfloat162*>(&xr);
#pragma unroll
      for (int j = 0; j < 4; ++j) {
        const float2 xv = __bfloat1622float2(xa[j]);
        acc[m] += xv.x * wv[2 * j] + xv.y * wv[2 * j + 1];
      }
    }
  }
#pragma unroll
  for (int m = 0; m < MAXM; ++m) {
    if (m >= M) break;
    float s = acc[m];
#pragma unroll
    for (int off = 16; off > 0; off >>= 1)
      s += __shfl_down_sync(0xffffffff, s, off);
    if (lane == 0)
      y[static_cast<size_t>(m) * N + n] = __bfloat16_as_ushort(__float2bfloat16(s));
  }
}

void LaunchFp8(int M, int blocks, const uint8_t* w, const float* sc,
               const uint16_t* x, uint16_t* y, int N, int K) {
  switch (M) {
    case 1: Fp8SmallMKernel<1><<<blocks, kThreads>>>(w, sc, x, y, M, N, K); break;
    case 2: Fp8SmallMKernel<2><<<blocks, kThreads>>>(w, sc, x, y, M, N, K); break;
    case 4: Fp8SmallMKernel<4><<<blocks, kThreads>>>(w, sc, x, y, M, N, K); break;
    case 8: Fp8SmallMKernel<8><<<blocks, kThreads>>>(w, sc, x, y, M, N, K); break;
    case 16: Fp8SmallMKernel<16><<<blocks, kThreads>>>(w, sc, x, y, M, N, K); break;
  }
}
void LaunchBf16(int M, int blocks, const uint16_t* w, const uint16_t* x,
                uint16_t* y, int N, int K) {
  switch (M) {
    case 1: Bf16SmallMKernel<1><<<blocks, kThreads>>>(w, x, y, M, N, K); break;
    case 2: Bf16SmallMKernel<2><<<blocks, kThreads>>>(w, x, y, M, N, K); break;
    case 4: Bf16SmallMKernel<4><<<blocks, kThreads>>>(w, x, y, M, N, K); break;
    case 8: Bf16SmallMKernel<8><<<blocks, kThreads>>>(w, x, y, M, N, K); break;
    case 16: Bf16SmallMKernel<16><<<blocks, kThreads>>>(w, x, y, M, N, K); break;
  }
}

float Bf16(float f) { return __bfloat162float(__float2bfloat16(f)); }
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
};

cublasLtHandle_t g_lt = nullptr;

// cuBLASLt BF16 GEMM, y[M,N] = x[M,K] @ W[N,K]^T (col-major: D[N,M] = op_T(W) *
// op_N(x)). Non-split-K (SM110a split-K reduce can write OOB). Returns us/call.
double BenchCublasBf16(const uint16_t* d_w_pool, int nbuf, size_t wstride_elems,
                       const uint16_t* d_x, uint16_t* d_y, int M, int N, int K,
                       void* ws, size_t ws_bytes) {
  cublasLtMatmulDesc_t op;
  cublasLtMatmulDescCreate(&op, CUBLAS_COMPUTE_32F, CUDA_R_32F);
  cublasOperation_t opT = CUBLAS_OP_T, opN = CUBLAS_OP_N;
  cublasLtMatmulDescSetAttribute(op, CUBLASLT_MATMUL_DESC_TRANSA, &opT, sizeof(opT));
  cublasLtMatmulDescSetAttribute(op, CUBLASLT_MATMUL_DESC_TRANSB, &opN, sizeof(opN));
  cublasLtMatrixLayout_t la, lb, lc;
  cublasLtMatrixLayoutCreate(&la, CUDA_R_16BF, K, N, K);
  cublasLtMatrixLayoutCreate(&lb, CUDA_R_16BF, K, M, K);
  cublasLtMatrixLayoutCreate(&lc, CUDA_R_16BF, N, M, N);
  cublasLtMatmulPreference_t pref;
  cublasLtMatmulPreferenceCreate(&pref);
  cublasLtMatmulPreferenceSetAttribute(
      pref, CUBLASLT_MATMUL_PREF_MAX_WORKSPACE_BYTES, &ws_bytes, sizeof(ws_bytes));
  uint32_t mask = CUBLASLT_REDUCTION_SCHEME_NONE;
  cublasLtMatmulPreferenceSetAttribute(
      pref, CUBLASLT_MATMUL_PREF_REDUCTION_SCHEME_MASK, &mask, sizeof(mask));
  cublasLtMatmulHeuristicResult_t heur;
  int nres = 0;
  cublasLtMatmulAlgoGetHeuristic(g_lt, op, la, lb, lc, lc, pref, 1, &heur, &nres);
  cublasLtMatmulPreferenceDestroy(pref);
  if (nres <= 0) {
    std::printf("  (cublas heuristic failed M=%d N=%d K=%d)\n", M, N, K);
    return -1.0;
  }
  const float alpha = 1.0f, beta = 0.0f;
  auto run = [&](int i) {
    cublasLtMatmul(g_lt, op, &alpha, d_w_pool + (i % nbuf) * wstride_elems, la,
                   d_x, lb, &beta, d_y, lc, d_y, lc, &heur.algo, ws, ws_bytes, 0);
  };
  for (int i = 0; i < 10; ++i) run(i);
  CK(cudaDeviceSynchronize());
  cudaEvent_t a, b;
  cudaEventCreate(&a);
  cudaEventCreate(&b);
  const int iters = 200;
  cudaEventRecord(a);
  for (int i = 0; i < iters; ++i) run(i);
  cudaEventRecord(b);
  CK(cudaEventSynchronize(b));
  float ms = 0;
  cudaEventElapsedTime(&ms, a, b);
  cudaEventDestroy(a);
  cudaEventDestroy(b);
  cublasLtMatrixLayoutDestroy(la);
  cublasLtMatrixLayoutDestroy(lb);
  cublasLtMatrixLayoutDestroy(lc);
  cublasLtMatmulDescDestroy(op);
  return ms / iters * 1000.0;
}

template <typename Launch>
double BenchKernel(Launch launch) {
  for (int i = 0; i < 10; ++i) launch(i);
  CK(cudaDeviceSynchronize());
  cudaEvent_t a, b;
  cudaEventCreate(&a);
  cudaEventCreate(&b);
  const int iters = 200;
  cudaEventRecord(a);
  for (int i = 0; i < iters; ++i) launch(i);
  cudaEventRecord(b);
  CK(cudaEventSynchronize(b));
  float ms = 0;
  cudaEventElapsedTime(&ms, a, b);
  cudaEventDestroy(a);
  cudaEventDestroy(b);
  return ms / iters * 1000.0;
}

void RunShape(const Shape& s) {
  const int N = s.N, K = s.K;
  std::mt19937 rng(1234 + N + K);
  std::normal_distribution<float> wd(0.0f, 0.02f), xd(0.0f, 1.0f);
  std::vector<float> Wf(static_cast<size_t>(N) * K);
  for (auto& v : Wf) v = wd(rng);
  std::vector<uint16_t> Wbf(Wf.size());
  for (size_t i = 0; i < Wf.size(); ++i) Wbf[i] = __bfloat16_as_ushort(__float2bfloat16(Wf[i]));
  std::vector<uint8_t> Wfp8(Wf.size());
  std::vector<float> wscale(N);
  for (int n = 0; n < N; ++n) {
    float amax = 0;
    for (int k = 0; k < K; ++k) amax = std::fmax(amax, std::fabs(Wf[(size_t)n * K + k]));
    const float sc = amax > 0 ? amax / 448.0f : 1.0f;
    wscale[n] = sc;
    for (int k = 0; k < K; ++k) {
      __nv_fp8_e4m3 q(Wf[(size_t)n * K + k] / sc);
      Wfp8[(size_t)n * K + k] = q.__x;
    }
  }

  const int kMaxM = 16;
  std::vector<float> xf(static_cast<size_t>(kMaxM) * K);
  std::vector<uint16_t> xh(xf.size());
  for (size_t i = 0; i < xf.size(); ++i) {
    const float v = xd(rng);
    xf[i] = Bf16(v);
    xh[i] = __bfloat16_as_ushort(__float2bfloat16(v));
  }

  // L2-defeat pools (working set > 32 MB L2) so the bench sees DRAM, not L2.
  const size_t kDefeat = 96ull << 20;
  const size_t wbf_bytes = Wbf.size() * 2, wfp_bytes = Wfp8.size();
  const int nbf = (int)std::max<size_t>(1, (kDefeat + wbf_bytes - 1) / wbf_bytes);
  const int nfp = (int)std::max<size_t>(1, (kDefeat + wfp_bytes - 1) / wfp_bytes);
  uint16_t* d_wbf = nullptr;
  uint8_t* d_wfp = nullptr;
  float* d_wsc = nullptr;
  uint16_t *d_x = nullptr, *d_y = nullptr;
  CK(cudaMalloc(&d_wbf, (size_t)nbf * wbf_bytes));
  CK(cudaMalloc(&d_wfp, (size_t)nfp * wfp_bytes));
  CK(cudaMalloc(&d_wsc, N * 4));
  CK(cudaMalloc(&d_x, xh.size() * 2));
  CK(cudaMalloc(&d_y, (size_t)kMaxM * N * 2));
  for (int i = 0; i < nbf; ++i)
    CK(cudaMemcpy(d_wbf + (size_t)i * Wbf.size(), Wbf.data(), wbf_bytes, cudaMemcpyHostToDevice));
  for (int i = 0; i < nfp; ++i)
    CK(cudaMemcpy(d_wfp + (size_t)i * Wfp8.size(), Wfp8.data(), wfp_bytes, cudaMemcpyHostToDevice));
  CK(cudaMemcpy(d_wsc, wscale.data(), N * 4, cudaMemcpyHostToDevice));
  CK(cudaMemcpy(d_x, xh.data(), xh.size() * 2, cudaMemcpyHostToDevice));
  void* ws = nullptr;
  const size_t ws_bytes = 32ull << 20;
  CK(cudaMalloc(&ws, ws_bytes));

  const int warps = kThreads / 32;
  const int blocks = (N + warps - 1) / warps;

  std::printf("== %s  N=%d K=%d ==\n", s.name, N, K);
  std::printf("  M | cuBLASLt-BF16 | hand-BF16      | hand-W8A16     | W8A16 vs cuBLAS | fp8 l2rel\n");
  const int Ms[] = {1, 2, 4, 8, 16};
  for (int M : Ms) {
    const double t_lt = BenchCublasBf16(d_wbf, nbf, Wbf.size(), d_x, d_y, M, N, K, ws, ws_bytes);
    const double t_bf = BenchKernel([&](int i) {
      LaunchBf16(M, blocks, d_wbf + (size_t)(i % nbf) * Wbf.size(), d_x, d_y, N, K);
    });
    const double t_fp = BenchKernel([&](int i) {
      LaunchFp8(M, blocks, d_wfp + (size_t)(i % nfp) * Wfp8.size(), d_wsc, d_x, d_y, N, K);
    });
    // Quality of the W8A16 kernel vs an FP32 reference (M rows).
    std::vector<uint16_t> yh((size_t)M * N);
    CK(cudaMemcpy(yh.data(), d_y, (size_t)M * N * 2, cudaMemcpyDeviceToHost));
    std::vector<float> yv((size_t)M * N), ref((size_t)M * N);
    for (size_t i = 0; i < yv.size(); ++i) yv[i] = __bfloat162float(__ushort_as_bfloat16(yh[i]));
    for (int m = 0; m < M; ++m)
      for (int n = 0; n < N; ++n) {
        double acc = 0;
        for (int k = 0; k < K; ++k) acc += (double)xf[(size_t)m * K + k] * Wf[(size_t)n * K + k];
        ref[(size_t)m * N + n] = (float)acc;
      }
    const double w_bf_gb = (double)N * K * 2 / 1e9, w_fp_gb = (double)N * K / 1e9;
    std::printf(
        "  %2d | %6.1fus %5.0fGB/s | %6.1fus %5.0fGB/s | %6.1fus %5.0fGB/s | %.2fx | %.2e\n",
        M, t_lt, t_lt > 0 ? w_bf_gb / (t_lt / 1e6) : 0, t_bf, w_bf_gb / (t_bf / 1e6),
        t_fp, w_fp_gb / (t_fp / 1e6), t_lt > 0 ? t_lt / t_fp : 0, L2Rel(yv, ref));
  }
  std::printf("\n");
  cudaFree(d_wbf);
  cudaFree(d_wfp);
  cudaFree(d_wsc);
  cudaFree(d_x);
  cudaFree(d_y);
  cudaFree(ws);
}

}  // namespace

int main() {
  std::printf("Small-M GEMM bench (Thor SM110a) - hand-written W8A16 vs cuBLASLt BF16\n");
  std::printf("weight N(0,0.02), act bf16 N(0,1), per-channel e4m3 scale, L2-defeated\n\n");
  cublasLtCreate(&g_lt);
  const Shape shapes[] = {
      {"qkv/o_proj", 2560, 2560},
      {"gate_up (GDN in)", 10240, 2560},
      {"down (GDN out)", 2560, 10240},
  };
  for (const auto& s : shapes) RunShape(s);
  cublasLtDestroy(g_lt);
  return 0;
}
