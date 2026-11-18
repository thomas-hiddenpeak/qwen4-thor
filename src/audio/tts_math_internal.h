// Scoped FP32 GEMM for native TTS. No TF32 or activation re-quantization.
#pragma once

#include <algorithm>
#include <mutex>
#include <string>
#include <utility>

#include "q4t/audio/tts_model.h"
#include "q4t/quant/lt_cache.h"

namespace q4t::audio::tts_detail {

constexpr int kThreads = 256;

__device__ inline float Read(const float* x, size_t i) { return x[i]; }

template <bool kMaximum>
__device__ float Reduce(float value, float* scratch) {
  scratch[threadIdx.x] = value;
  __syncthreads();
  for (int n = blockDim.x / 2; n; n /= 2) {
    if (threadIdx.x < n)
      scratch[threadIdx.x] =
          kMaximum ? fmaxf(scratch[threadIdx.x], scratch[threadIdx.x + n])
                   : scratch[threadIdx.x] + scratch[threadIdx.x + n];
    __syncthreads();
  }
  return scratch[0];
}

inline TtsComputeResult Invalid(const char* message) {
  return {Status::Fail(message), cudaSuccess};
}

inline TtsComputeResult Complete(Status status, cudaStream_t stream,
                                  cudaError_t submitted = cudaSuccess) {
  const auto launch = cudaPeekAtLastError();
  const auto complete = cudaStreamSynchronize(stream);
  const auto error = complete != cudaSuccess ? complete
                     : launch != cudaSuccess ? launch : submitted;
  if (error != cudaSuccess)
    return {Status::Fail(std::string("TTS CUDA failure: ") +
                         cudaGetErrorString(error)), error};
  return {std::move(status), cudaSuccess};
}

struct MatmulPlan {
  cublasLtMatmulDesc_t op = nullptr;
  cublasLtMatrixLayout_t a = nullptr, b = nullptr, c = nullptr;
  cublasLtMatmulPreference_t preference = nullptr;
  ~MatmulPlan() {
    if (preference) cublasLtMatmulPreferenceDestroy(preference);
    if (c) cublasLtMatrixLayoutDestroy(c);
    if (b) cublasLtMatrixLayoutDestroy(b);
    if (a) cublasLtMatrixLayoutDestroy(a);
    if (op) cublasLtMatmulDescDestroy(op);
  }
};

// Row-major output[M,N] = input[M,K] * weight[N,K]^T. Explicit leading
// dimension permits a bounded persistent V cache without hidden copies.
// Both weights and all activations remain FP32, including affine bias.
inline Status Matmul(const void* input, const void* weight, void* output,
                      int m, int n, int k, int weight_stride,
                      cudaDataType_t input_type, cudaDataType_t output_type,
                      void* workspace, size_t workspace_bytes,
                      cudaStream_t stream) {
  if (!input || !weight || !output || m < 1 || n < 1 || k < 1 ||
      weight_stride < k || !workspace ||
      input_type != CUDA_R_32F || output_type != CUDA_R_32F)
    return Status::Fail("TTS GEMM dimensions or buffers invalid");
  std::lock_guard<std::mutex> lock(quant::LtCacheMutex());
  const auto handle = quant::GlobalLtHandle();
  if (!handle) return Status::Fail("TTS cuBLAS handle unavailable");
  MatmulPlan p;
  cublasStatus_t status = CUBLAS_STATUS_SUCCESS;
  const auto check = [&](cublasStatus_t s) {
    if (status == CUBLAS_STATUS_SUCCESS) status = s;
    return s == CUBLAS_STATUS_SUCCESS;
  };
  const auto compute = CUBLAS_COMPUTE_32F_PEDANTIC;
  if (!check(cublasLtMatmulDescCreate(&p.op, compute, CUDA_R_32F)) ||
      !check(cublasLtMatrixLayoutCreate(&p.a, input_type, k, n, weight_stride)) ||
      !check(cublasLtMatrixLayoutCreate(&p.b, input_type, k, m, k)) ||
      !check(cublasLtMatrixLayoutCreate(&p.c, output_type, n, m, n)) ||
      !check(cublasLtMatmulPreferenceCreate(&p.preference)))
    return Status::Fail("TTS GEMM descriptor creation failed");
  const cublasOperation_t transpose = CUBLAS_OP_T;
  const uint32_t reduction = CUBLASLT_REDUCTION_SCHEME_NONE;
  if (!check(cublasLtMatmulDescSetAttribute(p.op,
          CUBLASLT_MATMUL_DESC_TRANSA, &transpose, sizeof(transpose))) ||
      !check(cublasLtMatmulPreferenceSetAttribute(p.preference,
          CUBLASLT_MATMUL_PREF_MAX_WORKSPACE_BYTES, &workspace_bytes,
          sizeof(workspace_bytes))) ||
      !check(cublasLtMatmulPreferenceSetAttribute(p.preference,
          CUBLASLT_MATMUL_PREF_REDUCTION_SCHEME_MASK, &reduction,
          sizeof(reduction))))
    return Status::Fail("TTS GEMM configuration failed");
  const auto alignment = [](const void* pointer) {
    const auto address = reinterpret_cast<uintptr_t>(pointer);
    return static_cast<uint32_t>(std::min(uintptr_t{256}, address & -address));
  };
  const uint32_t aa = alignment(weight), ba = alignment(input);
  const uint32_t ca = alignment(output);
  for (const auto& [attribute, value] : {
           std::pair{CUBLASLT_MATMUL_PREF_MIN_ALIGNMENT_A_BYTES, aa},
           std::pair{CUBLASLT_MATMUL_PREF_MIN_ALIGNMENT_B_BYTES, ba},
           std::pair{CUBLASLT_MATMUL_PREF_MIN_ALIGNMENT_C_BYTES, ca},
           std::pair{CUBLASLT_MATMUL_PREF_MIN_ALIGNMENT_D_BYTES, ca}})
    if (!check(cublasLtMatmulPreferenceSetAttribute(
            p.preference, attribute, &value, sizeof(value))))
      return Status::Fail("TTS GEMM alignment configuration failed");
  cublasLtMatmulHeuristicResult_t candidate{};
  int count = 0;
  if (!check(cublasLtMatmulAlgoGetHeuristic(handle, p.op, p.a, p.b, p.c, p.c,
          p.preference, 1, &candidate, &count)) || count != 1 ||
      candidate.state != CUBLAS_STATUS_SUCCESS)
    return Status::Fail("TTS GEMM algorithm unavailable");
  const float alpha = 1.0f, beta = 0.0f;
  check(cublasLtMatmul(handle, p.op, &alpha, weight, p.a, input, p.b,
                       &beta, output, p.c, output, p.c, &candidate.algo,
                       workspace, workspace_bytes, stream));
  return status == CUBLAS_STATUS_SUCCESS ? Status{}
      : Status::Fail("TTS GEMM failed: " + std::to_string(static_cast<int>(status)));
}

}  // namespace q4t::audio::tts_detail
