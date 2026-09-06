// M=1 BF16 GEMV (y[N] = alpha * sum_k x[k] * W[N, K]) — decode path.
//
// Dispatched from q4t::model::Bf16Gemm when M == 1. Reads W at full DRAM
// bandwidth (vs cuBLASLt's GEMM tiling which underutilizes DRAM for M=1).
// Requires K % 8 == 0 (all qwen4_exp projections satisfy this).
#pragma once

#include <cuda_runtime.h>

#include <cstdint>

namespace q4t {
namespace model {

// y[N] = alpha * (x[1, K] * W[N, K]^T). beta must be 0 (handled by caller).
//   x   : device row-major [1, K] uint16 (BF16)
//   w   : device row-major [N, K] uint16 (BF16)
//   y   : device row-major [N] uint16 (BF16), caller-allocated
//   alpha : FP32 scale
//   stream : CUDA stream
// Deterministic (fixed per-block reduction order, no atomics) so repeated
// decode steps are bit-stable. Returns false if unsupported (K % 8 != 0) or
// on launch failure; caller must fall back to the cuBLASLt GEMM path.
bool Bf16Gev(const uint16_t* x, const uint16_t* w, uint16_t* y, int N, int K,
             float alpha, cudaStream_t stream);

}  // namespace model
}  // namespace q4t
