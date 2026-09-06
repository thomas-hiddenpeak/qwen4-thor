// Native NVFP4 W4A4 GEMM via cuBLASLt (CUDA_R_4F_E2M1 + VEC16_UE4M3).
//
// Computes C[M, N] = A[M, K] * W[N, K]^T in FP32, where both A (activations)
// and W (weights) are NVFP4. This is the hardware path requested for
// qwen4_exp routed experts (W4A4): weights stay packed, activations are
// quantized at runtime (act_quant.h), and the matmul runs on the SM110a
// tensor cores.
//
// Scale convention (matches the ModelOpt checkpoint and qwen35-thor):
//   W_real = e2m1 * e4m3_group_scale * inv_w_global   (inv_w_global = w_scale_2)
//   A_real = e2m1 * e4m3_group_scale * inv_a_global   (inv_a_global = input_scale)
// The two FP32 global scales are folded into alpha = inv_w_global *
// inv_a_global. The per-group e4m3 scale tensors must be in the swizzled
// layout (swizzle.h); the FP4 payloads are row-major.
//
// Verified on Thor SM110a: 16/16 real expert-projection shapes (N=640/K=2560
// and N=2560/K=640, M=1..256) match a CPU dequant reference with max_rel
// < 1e-4.
#pragma once

#include <cublasLt.h>

#include <cstdint>

#include "q4t/quant/lt_cache.h"

namespace q4t {
namespace quant {

// Result of a native NVFP4 GEMM.
struct Fp4GemmResult {
  cublasStatus_t status = CUBLAS_STATUS_SUCCESS;
  bool has_algo = false;
};

// Run C[M, N] = A[M, K] * W[N, K]^T with NVFP4 inputs.
//
//   w_packed   : row-major [N, K/2] uint8 (packed e2m1 weights)
//   w_sf       : swizzled e4m3 weight scales, >= SfBufferSize(N, K) bytes
//   a_packed   : row-major [M, K/2] uint8 (packed e2m1 activations)
//   a_sf       : swizzled e4m3 activation scales, >= SfBufferSize(M, K) bytes
//   out        : row-major [M, N] float32, caller-allocated
//   inv_w_global : 1 / weight global scale (= weight_scale_2, ModelOpt)
//   inv_a_global : 1 / activation global scale (= input_scale)
//   workspace  : device buffer of at least `workspace_bytes`
//   workspace_bytes : size of `workspace`
//   stream     : CUDA stream
//
// K must be a multiple of 32 (cuBLASLt FP4 requirement). Returns a result
// struct; check .status and .has_algo.
//
// The cuBLASLt descriptor/layouts/algorithm are cached per (M, N, K,
// workspace_bytes) in the shared plan cache (lt_cache.h). The two scale
// pointers are re-pointed on every call because the same shape is reused
// across experts with different scale buffers.
inline Fp4GemmResult Fp4Gemm(const uint8_t* w_packed, const uint8_t* w_sf,
                             const uint8_t* a_packed, const uint8_t* a_sf,
                             float* out, int M, int N, int K,
                             float inv_w_global, float inv_a_global,
                             void* workspace, size_t workspace_bytes,
                             cudaStream_t stream) {
  Fp4GemmResult res;
  std::lock_guard<std::mutex> lock(LtCacheMutex());
  LtPlanKey key{M, N, K, workspace_bytes};
  LtPlanMap& cache = Fp4PlanCache();
  LtPlan* plan;
  auto it = cache.find(key);
  if (it != cache.end()) {
    plan = &it->second;
  } else {
    cublasLtHandle_t handle = GlobalLtHandle();
    if (handle == nullptr) {
      res.status = CUBLAS_STATUS_NOT_INITIALIZED;
      return res;
    }
    LtPlan np;
    cublasStatus_t s = cublasLtMatmulDescCreate(&np.op, CUBLAS_COMPUTE_32F,
                                                CUDA_R_32F);
    if (s != CUBLAS_STATUS_SUCCESS) {
      res.status = s;
      return res;
    }
    // cuBLASLt is column-major. We express D = alpha * op(A_slot) * op(B_slot)
    // with A_slot = W (col-major [K, N], OP_T) and B_slot = act (col-major
    // [K, M], OP_N), giving D (col-major [N, M]) == row-major [M, N].
    cublasOperation_t op_w = CUBLAS_OP_T;
    cublasOperation_t op_a = CUBLAS_OP_N;
    cublasLtMatmulDescSetAttribute(np.op, CUBLASLT_MATMUL_DESC_TRANSA, &op_w,
                                   sizeof(op_w));
    cublasLtMatmulDescSetAttribute(np.op, CUBLASLT_MATMUL_DESC_TRANSB, &op_a,
                                   sizeof(op_a));

    cublasLtMatmulMatrixScale_t sm = CUBLASLT_MATMUL_MATRIX_SCALE_VEC16_UE4M3;
    cublasLtMatmulDescSetAttribute(np.op, CUBLASLT_MATMUL_DESC_A_SCALE_MODE,
                                   &sm, sizeof(sm));
    cublasLtMatmulDescSetAttribute(np.op, CUBLASLT_MATMUL_DESC_B_SCALE_MODE,
                                   &sm, sizeof(sm));
    // The heuristic requires the scale pointers to be set on the descriptor;
    // point them at this call's buffers now (they are re-pointed on every
    // subsequent call below).
    {
      const void* d_w_sf = w_sf;
      const void* d_a_sf = a_sf;
      cublasLtMatmulDescSetAttribute(np.op, CUBLASLT_MATMUL_DESC_A_SCALE_POINTER,
                                     &d_w_sf, sizeof(d_w_sf));
      cublasLtMatmulDescSetAttribute(np.op, CUBLASLT_MATMUL_DESC_B_SCALE_POINTER,
                                     &d_a_sf, sizeof(d_a_sf));
    }

    // A slot = W: stored col-major [K, N] (ld = K).
    cublasLtMatrixLayoutCreate(&np.la, CUDA_R_4F_E2M1, K, N, K);
    // B slot = act: stored col-major [K, M] (ld = K).
    cublasLtMatrixLayoutCreate(&np.lb, CUDA_R_4F_E2M1, K, M, K);
    // C: col-major [N, M] (ld = N).
    cublasLtMatrixLayoutCreate(&np.lc, CUDA_R_32F, N, M, N);

    cublasLtMatmulPreference_t pref;
    cublasLtMatmulPreferenceCreate(&pref);
    cublasLtMatmulPreferenceSetAttribute(
        pref, CUBLASLT_MATMUL_PREF_MAX_WORKSPACE_BYTES, &workspace_bytes,
        sizeof(workspace_bytes));
    cublasLtMatmulHeuristicResult_t heur;
    int nres = 0;
    s = cublasLtMatmulAlgoGetHeuristic(handle, np.op, np.la, np.lb, np.lc,
                                       np.lc, pref, 1, &heur, &nres);
    cublasLtMatmulPreferenceDestroy(pref);
    if (s != CUBLAS_STATUS_SUCCESS || nres <= 0) {
      res.status = s;
      res.has_algo = false;
      cublasLtMatrixLayoutDestroy(np.la);
      cublasLtMatrixLayoutDestroy(np.lb);
      cublasLtMatrixLayoutDestroy(np.lc);
      cublasLtMatmulDescDestroy(np.op);
      return res;
    }
    np.algo = heur.algo;
    np.has_algo = true;
    auto inserted = cache.emplace(key, np);
    plan = &inserted.first->second;
  }

  // Re-point the scale buffers (cheap; they differ per expert per call).
  const void* d_w_sf = w_sf;
  const void* d_a_sf = a_sf;
  cublasLtMatmulDescSetAttribute(plan->op, CUBLASLT_MATMUL_DESC_A_SCALE_POINTER,
                                 &d_w_sf, sizeof(d_w_sf));
  cublasLtMatmulDescSetAttribute(plan->op, CUBLASLT_MATMUL_DESC_B_SCALE_POINTER,
                                 &d_a_sf, sizeof(d_a_sf));

  const float alpha = inv_w_global * inv_a_global;
  const float beta = 0.0f;
  res.has_algo = true;
  res.status = cublasLtMatmul(GlobalLtHandle(), plan->op, &alpha, w_packed,
                              plan->la, a_packed, plan->lb, &beta, out,
                              plan->lc, out, plan->lc, &plan->algo, workspace,
                              workspace_bytes, stream);
  return res;
}

}  // namespace quant
}  // namespace q4t
