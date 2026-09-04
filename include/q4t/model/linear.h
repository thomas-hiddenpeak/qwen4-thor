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

namespace q4t {
namespace model {

// Result of a BF16 GEMM.
struct Bf16GemmResult {
  cublasStatus_t status = CUBLAS_STATUS_SUCCESS;
  bool has_algo = false;
};

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
  cublasLtHandle_t handle;
  if (cublasLtCreate(&handle) != CUBLAS_STATUS_SUCCESS) {
    res.status = CUBLAS_STATUS_NOT_INITIALIZED;
    return res;
  }
  cublasLtMatmulDesc_t op;
  cublasStatus_t s =
      cublasLtMatmulDescCreate(&op, CUBLAS_COMPUTE_32F, CUDA_R_32F);
  if (s != CUBLAS_STATUS_SUCCESS) {
    res.status = s;
    cublasLtDestroy(handle);
    return res;
  }
  cublasOperation_t op_w = CUBLAS_OP_T;
  cublasOperation_t op_x = CUBLAS_OP_N;
  cublasLtMatmulDescSetAttribute(op, CUBLASLT_MATMUL_DESC_TRANSA, &op_w,
                                 sizeof(op_w));
  cublasLtMatmulDescSetAttribute(op, CUBLASLT_MATMUL_DESC_TRANSB, &op_x,
                                 sizeof(op_x));

  cublasLtMatrixLayout_t la, lb, lc;
  // A slot = W: col-major [K, N] (ld = K), OP_T -> [N, K].
  cublasLtMatrixLayoutCreate(&la, CUDA_R_16BF, K, N, K);
  // B slot = x: col-major [K, M] (ld = K), OP_N -> [K, M].
  cublasLtMatrixLayoutCreate(&lb, CUDA_R_16BF, K, M, K);
  // C: col-major [N, M] (ld = N) == row-major [M, N].
  cublasLtMatrixLayoutCreate(&lc, CUDA_R_16BF, N, M, N);

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
  cublasLtMatmulPreferenceSetAttribute(pref,
                                       CUBLASLT_MATMUL_PREF_REDUCTION_SCHEME_MASK,
                                       &reduction_mask, sizeof(reduction_mask));
  cublasLtMatmulHeuristicResult_t heur;
  int nres = 0;
  s = cublasLtMatmulAlgoGetHeuristic(handle, op, la, lb, lc, lc, pref, 1,
                                     &heur, &nres);
  if (s != CUBLAS_STATUS_SUCCESS || nres <= 0) {
    res.status = s;
    res.has_algo = false;
    cublasLtMatmulPreferenceDestroy(pref);
    cublasLtMatrixLayoutDestroy(la);
    cublasLtMatrixLayoutDestroy(lb);
    cublasLtMatrixLayoutDestroy(lc);
    cublasLtMatmulDescDestroy(op);
    cublasLtDestroy(handle);
    return res;
  }
  res.has_algo = true;
  s = cublasLtMatmul(handle, op, &alpha, w, la, x, lb, &beta, y, lc, y, lc,
                     &heur.algo, workspace, workspace_bytes, stream);
  res.status = s;

  cublasLtMatmulPreferenceDestroy(pref);
  cublasLtMatrixLayoutDestroy(la);
  cublasLtMatrixLayoutDestroy(lb);
  cublasLtMatrixLayoutDestroy(lc);
  cublasLtMatmulDescDestroy(op);
  cublasLtDestroy(handle);
  return res;
}

}  // namespace model
}  // namespace q4t
