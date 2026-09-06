// BF16 linear layer (y = x * W^T) via cuBLASLt.
//
// The general-purpose BF16 GEMM primitive for the model layer: attention
// projections, hyper-connection mix/combine, shared expert, lm_head, etc.
// (NVFP4 routed experts use q4t::quant::Fp4Gemm instead.)
//
// Weight `W` is stored row-major [N, K] (checkpoint layout: out_features x
// in_features, no bias). The GEMM computes, in cuBLASLt column-major terms,
//   D (col-major [N, M]) = alpha * op(A=W, OP_T) * op(B=x, OP_N) + beta * C
// which is the row-major [M, N] result  y[m, n] = sum_k x[m, k] * W[n, k].
#pragma once

#include <cublasLt.h>

#include <cstdint>

#include "q4t/model/gemv.h"
#include "q4t/quant/lt_cache.h"

namespace q4t {
namespace model {

// Result of a BF16 GEMM.
struct Bf16GemmResult {
  cublasStatus_t status = CUBLAS_STATUS_SUCCESS;
  bool has_algo = false;
};

// Build (or fetch from cache) the cuBLASLt plan for a BF16 GEMM of shape
// (M, N, K) with the given workspace budget. On a cache miss this creates the
// descriptor + three layouts and runs the heuristic once; on a hit it returns
// the cached plan. The caller must hold LtCacheMutex().
inline quant::LtPlan* GetBf16Plan(int M, int N, int K, size_t workspace_bytes) {
  quant::LtPlanKey key{M, N, K, workspace_bytes};
  quant::LtPlanMap& cache = quant::Bf16PlanCache();
  auto it = cache.find(key);
  if (it != cache.end()) return &it->second;

  cublasLtHandle_t handle = quant::GlobalLtHandle();
  if (handle == nullptr) return nullptr;

  quant::LtPlan plan;
  cublasStatus_t s =
      cublasLtMatmulDescCreate(&plan.op, CUBLAS_COMPUTE_32F, CUDA_R_32F);
  if (s != CUBLAS_STATUS_SUCCESS) return nullptr;
  cublasOperation_t op_w = CUBLAS_OP_T;
  cublasOperation_t op_x = CUBLAS_OP_N;
  cublasLtMatmulDescSetAttribute(plan.op, CUBLASLT_MATMUL_DESC_TRANSA, &op_w,
                                 sizeof(op_w));
  cublasLtMatmulDescSetAttribute(plan.op, CUBLASLT_MATMUL_DESC_TRANSB, &op_x,
                                 sizeof(op_x));

  // A slot = W: col-major [K, N] (ld = K), OP_T -> [N, K].
  cublasLtMatrixLayoutCreate(&plan.la, CUDA_R_16BF, K, N, K);
  // B slot = x: col-major [K, M] (ld = K), OP_N -> [K, M].
  cublasLtMatrixLayoutCreate(&plan.lb, CUDA_R_16BF, K, M, K);
  // C: col-major [N, M] (ld = N) == row-major [M, N].
  cublasLtMatrixLayoutCreate(&plan.lc, CUDA_R_16BF, N, M, N);

  cublasLtMatmulPreference_t pref;
  cublasLtMatmulPreferenceCreate(&pref);
  cublasLtMatmulPreferenceSetAttribute(pref,
                                       CUBLASLT_MATMUL_PREF_MAX_WORKSPACE_BYTES,
                                       &workspace_bytes, sizeof(workspace_bytes));
  // Disable split-K: for tall-skinny shapes (small M, large K) cuBLASLt picks
  // a split-K algorithm whose splitKreduce_kernel writes out of bounds on
  // SM110a (verified via compute-sanitizer on the router GEMM). Non-split-K
  // algorithms are always correct; split-K is only a perf optimization.
  uint32_t reduction_mask = CUBLASLT_REDUCTION_SCHEME_NONE;
  cublasLtMatmulPreferenceSetAttribute(
      pref, CUBLASLT_MATMUL_PREF_REDUCTION_SCHEME_MASK, &reduction_mask,
      sizeof(reduction_mask));
  cublasLtMatmulHeuristicResult_t heur;
  int nres = 0;
  s = cublasLtMatmulAlgoGetHeuristic(handle, plan.op, plan.la, plan.lb, plan.lc,
                                     plan.lc, pref, 1, &heur, &nres);
  cublasLtMatmulPreferenceDestroy(pref);
  if (s != CUBLAS_STATUS_SUCCESS || nres <= 0) {
    // Leave the partially-built plan out of the cache; caller sees has_algo
    // false. Clean up the objects we created.
    cublasLtMatrixLayoutDestroy(plan.la);
    cublasLtMatrixLayoutDestroy(plan.lb);
    cublasLtMatrixLayoutDestroy(plan.lc);
    cublasLtMatmulDescDestroy(plan.op);
    return nullptr;
  }
  plan.algo = heur.algo;
  plan.has_algo = true;
  auto inserted = cache.emplace(key, plan);
  return &inserted.first->second;
}

// y[M, N] = x[M, K] * W[N, K]^T  (BF16 in/out, FP32 accumulate).
//
//   x   : device row-major [M, K] uint16 (BF16)
//   w   : device row-major [N, K] uint16 (BF16)
//   y   : device row-major [M, N] uint16 (BF16), caller-allocated
//   alpha, beta : FP32 scalars (beta typically 0)
//   workspace   : device buffer of at least `workspace_bytes`
//   stream      : CUDA stream
inline Bf16GemmResult Bf16Gemm(const uint16_t* x, const uint16_t* w,
                               uint16_t* y, int M, int N, int K, float alpha,
                               float beta, void* workspace,
                               size_t workspace_bytes, cudaStream_t stream) {
  Bf16GemmResult res;
  // M=1 (decode): dedicated GEMV kernel — reads W at full DRAM bandwidth.
  // beta must be 0 (all call sites use 0).
  if (M == 1 && beta == 0.0f) {
    if (Bf16Gev(x, w, y, N, K, alpha, stream)) {
      res.status = CUBLAS_STATUS_SUCCESS;
      res.has_algo = true;
      return res;
    }
    // Unsupported shape (K % 8 != 0) or launch failure: fall through to
    // the cuBLASLt path.
  }
  std::lock_guard<std::mutex> lock(quant::LtCacheMutex());
  quant::LtPlan* plan = GetBf16Plan(M, N, K, workspace_bytes);
  if (plan == nullptr || !plan->has_algo) {
    res.status = CUBLAS_STATUS_NOT_INITIALIZED;
    res.has_algo = false;
    return res;
  }
  res.has_algo = true;
  res.status = cublasLtMatmul(quant::GlobalLtHandle(), plan->op, &alpha, w,
                              plan->la, x, plan->lb, &beta, y, plan->lc, y,
                              plan->lc, &plan->algo, workspace, workspace_bytes,
                              stream);
  return res;
}

}  // namespace model
}  // namespace q4t
