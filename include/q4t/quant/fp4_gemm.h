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
inline Fp4GemmResult Fp4Gemm(const uint8_t* w_packed, const uint8_t* w_sf,
                             const uint8_t* a_packed, const uint8_t* a_sf,
                             float* out, int M, int N, int K,
                             float inv_w_global, float inv_a_global,
                             void* workspace, size_t workspace_bytes,
                             cudaStream_t stream) {
  Fp4GemmResult res;
  cublasLtHandle_t handle;
  if (cublasLtCreate(&handle) != CUBLAS_STATUS_SUCCESS) {
    res.status = CUBLAS_STATUS_NOT_INITIALIZED;
    return res;
  }

  cublasLtMatmulDesc_t op;
  cublasStatus_t s = cublasLtMatmulDescCreate(&op, CUBLAS_COMPUTE_32F,
                                              CUDA_R_32F);
  if (s != CUBLAS_STATUS_SUCCESS) {
    res.status = s;
    cublasLtDestroy(handle);
    return res;
  }
  // cuBLASLt is column-major. We express D = alpha * op(A_slot) * op(B_slot)
  // with A_slot = W (col-major [K, N], OP_T) and B_slot = act (col-major [K,
  // M], OP_N), giving D (col-major [N, M]) == row-major [M, N].
  cublasOperation_t op_w = CUBLAS_OP_T;
  cublasOperation_t op_a = CUBLAS_OP_N;
  cublasLtMatmulDescSetAttribute(op, CUBLASLT_MATMUL_DESC_TRANSA, &op_w,
                                 sizeof(op_w));
  cublasLtMatmulDescSetAttribute(op, CUBLASLT_MATMUL_DESC_TRANSB, &op_a,
                                 sizeof(op_a));

  cublasLtMatmulMatrixScale_t sm = CUBLASLT_MATMUL_MATRIX_SCALE_VEC16_UE4M3;
  cublasLtMatmulDescSetAttribute(op, CUBLASLT_MATMUL_DESC_A_SCALE_MODE, &sm,
                                 sizeof(sm));
  cublasLtMatmulDescSetAttribute(op, CUBLASLT_MATMUL_DESC_B_SCALE_MODE, &sm,
                                 sizeof(sm));
  const void* d_w_sf = w_sf;
  const void* d_a_sf = a_sf;
  cublasLtMatmulDescSetAttribute(op, CUBLASLT_MATMUL_DESC_A_SCALE_POINTER,
                                 &d_w_sf, sizeof(d_w_sf));
  cublasLtMatmulDescSetAttribute(op, CUBLASLT_MATMUL_DESC_B_SCALE_POINTER,
                                 &d_a_sf, sizeof(d_a_sf));

  const float alpha = inv_w_global * inv_a_global;
  const float beta = 0.0f;

  cublasLtMatrixLayout_t la, lb, lc;
  // A slot = W: stored col-major [K, N] (ld = K).
  cublasLtMatrixLayoutCreate(&la, CUDA_R_4F_E2M1, K, N, K);
  // B slot = act: stored col-major [K, M] (ld = K).
  cublasLtMatrixLayoutCreate(&lb, CUDA_R_4F_E2M1, K, M, K);
  // C: col-major [N, M] (ld = N).
  cublasLtMatrixLayoutCreate(&lc, CUDA_R_32F, N, M, N);

  cublasLtMatmulPreference_t pref;
  cublasLtMatmulPreferenceCreate(&pref);
  cublasLtMatmulPreferenceSetAttribute(pref,
                                       CUBLASLT_MATMUL_PREF_MAX_WORKSPACE_BYTES,
                                       &workspace_bytes,
                                       sizeof(workspace_bytes));

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

  s = cublasLtMatmul(handle, op, &alpha, w_packed, la, a_packed, lb, &beta,
                     out, lc, out, lc, &heur.algo, workspace, workspace_bytes,
                     stream);
  res.status = s;

  cublasLtMatmulPreferenceDestroy(pref);
  cublasLtMatrixLayoutDestroy(la);
  cublasLtMatrixLayoutDestroy(lb);
  cublasLtMatrixLayoutDestroy(lc);
  cublasLtMatmulDescDestroy(op);
  cublasLtDestroy(handle);
  return res;
}

}  // namespace quant
}  // namespace q4t
