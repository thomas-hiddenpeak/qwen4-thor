// GEMM roofline benchmark: measure cuBLASLt BF16 TFLOPS on the model's real
// dense GEMM shapes across T (sequence length), vs the SM110a BF16 peak.
//
// Purpose: establish the "Pole 2" (compute roof) baseline for the
// data-flow-driven optimization. The two-pole framework
// (docs/DATAFLOW_OPTIMIZATION.md §0.0.3) says compute-bound GEMMs (prefill
// large M) should be hand-written tensor core (tcgen05.mma) to >50% of peak,
// because the library (cuBLASLt/nvjet) achieves <10% of peak on NVFP4. This
// tool measures the ACTUAL library utilization on OUR BF16 shapes — the
// "ruler" that determines how big Pole 2 really is before we commit to
// hand-writing any GEMM.
//
// Faithfully replicates the model's Bf16Gemm path (include/q4t/model/linear.h):
//   - CUBLAS_COMPUTE_32F, BF16 in/out, FP32 accumulate
//   - A slot = W [N,K] row-major (col-major [K,N] ld=K) OP_T
//   - B slot = x [M,K] row-major (col-major [K,M] ld=K) OP_N
//   - C slot = y [M,N] row-major (col-major [N,M] ld=N)
//   - split-K DISABLED (SM110a split-Kreduce OOB bug, see linear.h)
//   - M=1 decode path uses Bf16Gev GEMV in the model; here we measure the
//     cuBLASLt M=1 GEMM for the pure-library comparison (GEMV is bandwidth-
//     bound and already closed at the floor).
//
// Shapes (docs/MODEL.md): hidden=2560, GDN in_qkv=10240 in_z=6144,
// QSA qg=nq*hd=6144+2*512=7168... (see kShapes), HC lowrank=320,
// vocab=248320.
//
// Build: cmake --build build --target q4t_gemm_roofline_bench
// Run:   ./build/q4t_gemm_roofline_bench
#include <cublasLt.h>
#include <cuda_bf16.h>
#include <cuda_runtime.h>

#include <algorithm>
#include <cfloat>
#include <cinttypes>
#include <cstddef>
#include <cstdio>
#include <cstdlib>
#include <string>
#include <vector>

namespace {

constexpr float kBf16PeakTflops = 259.0f;  // SM110a BF16 dense peak (TFLOPS).
constexpr int kWarmup = 3;
constexpr int kIters = 20;
constexpr size_t kWorkspaceBytes = 32u << 20;  // 32 MiB cuBLASLt workspace.

struct Ctx {
  cublasLtHandle_t handle = nullptr;
  void* ws = nullptr;
};

[[noreturn]] void Die(const char* msg) {
  std::fprintf(stderr, "gemm_roofline_bench: %s\n", msg);
  std::exit(1);
}

#define CUDA_CHECK(x)                                        \
  do {                                                       \
    cudaError_t e_ = (x);                                    \
    if (e_ != cudaSuccess) {                                 \
      Die(cudaGetErrorString(e_));                           \
    }                                                        \
  } while (0)

#define CUBLAS_CHECK(x)                                                  \
  do {                                                                   \
    cublasStatus_t s_ = (x);                                             \
    if (s_ != CUBLAS_STATUS_SUCCESS) {                                   \
      std::fprintf(stderr, "cublasLt error %d at line %d\n", (int)s_,    \
                   __LINE__);                                            \
      std::exit(1);                                                      \
    }                                                                    \
  } while (0)

struct Shape {
  const char* name;
  int N;  // out_features
  int K;  // in_features
};

// The model's real dense BF16 GEMM shapes (per forward, per layer where
// noted). M = T (sequence length) is swept separately.
const Shape kShapes[] = {
    {"GDN in_proj_qkv (36L)", 10240, 2560},
    {"GDN in_proj_z (36L)", 6144, 2560},
    {"GDN out_proj (36L)", 2560, 6144},
    {"QSA qg (12L)", 7168, 2560},
    {"QSA o_proj (12L)", 2560, 6144},
    {"HC down (48x2)", 320, 10240},
    {"HC up (48x2)", 10240, 320},
    {"HC inject (48x2)", 10240, 2560},
    {"lm_head (1)", 248320, 2560},
};

const int kTValues[] = {1, 8, 512, 8192};

// One cuBLASLt BF16 GEMM: y[M,N] = x[M,K] * W[N,K]^T, FP32 accumulate,
// split-K disabled (matches linear.h Bf16Gemm).
float RunGemm(Ctx& ctx, const uint16_t* x, const uint16_t* w, uint16_t* y,
              int M, int N, int K) {
  cublasOperation_t op_t = CUBLAS_OP_T;
  cublasOperation_t op_n = CUBLAS_OP_N;
  cublasLtMatmulDesc_t op;
  CUBLAS_CHECK(cublasLtMatmulDescCreate(&op, CUBLAS_COMPUTE_32F, CUDA_R_32F));
  cublasLtMatmulDescSetAttribute(op, CUBLASLT_MATMUL_DESC_TRANSA, &op_t,
                                 sizeof(op_t));
  cublasLtMatmulDescSetAttribute(op, CUBLASLT_MATMUL_DESC_TRANSB, &op_n,
                                 sizeof(op_n));
  cublasLtMatrixLayout_t la, lb, lc;
  CUBLAS_CHECK(cublasLtMatrixLayoutCreate(&la, CUDA_R_16BF, K, N, K));
  CUBLAS_CHECK(cublasLtMatrixLayoutCreate(&lb, CUDA_R_16BF, K, M, K));
  CUBLAS_CHECK(cublasLtMatrixLayoutCreate(&lc, CUDA_R_16BF, N, M, N));
  cublasLtMatmulPreference_t pref;
  CUBLAS_CHECK(cublasLtMatmulPreferenceCreate(&pref));
  cublasLtMatmulPreferenceSetAttribute(pref,
                                       CUBLASLT_MATMUL_PREF_MAX_WORKSPACE_BYTES,
                                       &kWorkspaceBytes,
                                       sizeof(kWorkspaceBytes));
  uint32_t no_splitk = CUBLASLT_REDUCTION_SCHEME_NONE;
  cublasLtMatmulPreferenceSetAttribute(
      pref, CUBLASLT_MATMUL_PREF_REDUCTION_SCHEME_MASK, &no_splitk,
      sizeof(no_splitk));
  cublasLtMatmulHeuristicResult_t heur;
  int nres = 0;
  CUBLAS_CHECK(cublasLtMatmulAlgoGetHeuristic(ctx.handle, op, la, lb, lc, lc,
                                              pref, 1, &heur, &nres));
  cublasLtMatmulPreferenceDestroy(pref);
  if (nres <= 0) {
    cublasLtMatrixLayoutDestroy(la);
    cublasLtMatrixLayoutDestroy(lb);
    cublasLtMatrixLayoutDestroy(lc);
    cublasLtMatmulDescDestroy(op);
    return -1.0f;  // no algo
  }
  const float alpha = 1.0f, beta = 0.0f;
  // Warmup.
  for (int i = 0; i < kWarmup; ++i) {
    CUBLAS_CHECK(cublasLtMatmul(ctx.handle, op, &alpha, w, la, x, lb, &beta,
                                y, lc, y, lc, &heur.algo, ctx.ws,
                                kWorkspaceBytes, 0));
  }
  cudaEvent_t e0, e1;
  CUDA_CHECK(cudaEventCreate(&e0));
  CUDA_CHECK(cudaEventCreate(&e1));
  float best = FLT_MAX;
  for (int i = 0; i < kIters; ++i) {
    CUDA_CHECK(cudaEventRecord(e0, 0));
    CUBLAS_CHECK(cublasLtMatmul(ctx.handle, op, &alpha, w, la, x, lb, &beta,
                                y, lc, y, lc, &heur.algo, ctx.ws,
                                kWorkspaceBytes, 0));
    CUDA_CHECK(cudaEventRecord(e1, 0));
    CUDA_CHECK(cudaEventSynchronize(e1));
    float ms = 0.0f;
    CUDA_CHECK(cudaEventElapsedTime(&ms, e0, e1));
    best = std::min(best, ms);
  }
  CUDA_CHECK(cudaEventDestroy(e0));
  CUDA_CHECK(cudaEventDestroy(e1));
  cublasLtMatrixLayoutDestroy(la);
  cublasLtMatrixLayoutDestroy(lb);
  cublasLtMatrixLayoutDestroy(lc);
  cublasLtMatmulDescDestroy(op);
  return best;  // ms
}

}  // namespace

int main() {
  Ctx ctx;
  CUBLAS_CHECK(cublasLtCreate(&ctx.handle));
  CUDA_CHECK(cudaMalloc(&ctx.ws, kWorkspaceBytes));

  int dev = 0;
  cudaDeviceProp prop;
  CUDA_CHECK(cudaGetDeviceProperties(&prop, dev));
  std::printf("Device: %s (SM %d%d, %d SMs)\n", prop.name, prop.major,
              prop.minor, prop.multiProcessorCount);
  std::printf("BF16 dense peak: %.0f TFLOPS\n\n", kBf16PeakTflops);

  std::printf(
      "%-22s %8s %12s %10s %9s %9s\n", "shape", "T", "ms", "TFLOPS",
      "%peak", "GB/s(x)");
  std::printf("%s\n", std::string(72, '-').c_str());

  for (const Shape& sh : kShapes) {
    for (int T : kTValues) {
      const int M = T, N = sh.N, K = sh.K;
      const size_t x_bytes = (size_t)M * K * 2;
      const size_t w_bytes = (size_t)N * K * 2;
      const size_t y_bytes = (size_t)M * N * 2;
      uint16_t *x = nullptr, *w = nullptr, *y = nullptr;
      CUDA_CHECK(cudaMalloc(&x, x_bytes));
      CUDA_CHECK(cudaMalloc(&w, w_bytes));
      CUDA_CHECK(cudaMalloc(&y, y_bytes));
      CUDA_CHECK(cudaMemset(x, 0x3c, x_bytes));  // arbitrary BF16 pattern
      CUDA_CHECK(cudaMemset(w, 0x3c, w_bytes));
      float ms = RunGemm(ctx, x, w, y, M, N, K);
      CUDA_CHECK(cudaFree(x));
      CUDA_CHECK(cudaFree(w));
      CUDA_CHECK(cudaFree(y));
      if (ms < 0) {
        std::printf("%-22s %8d   (no algo)\n", sh.name, T);
        continue;
      }
      double flops = 2.0 * M * N * K;
      double tflops = flops / (ms * 1e-3) / 1e12;
      double pct = tflops / kBf16PeakTflops * 100.0;
      double bytes = x_bytes + w_bytes + y_bytes;
      double gbps = bytes / (ms * 1e-3) / 1e9;
      std::printf("%-22s %8d %12.4f %10.1f %8.1f%% %9.0f\n", sh.name, T, ms,
                  tflops, pct, gbps);
    }
  }
  cublasLtDestroy(ctx.handle);
  CUDA_CHECK(cudaFree(ctx.ws));
  return 0;
}
