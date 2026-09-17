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

// FP8 (e4m3) shadow of a BF16 [N, K] projection weight, for the M=1 decode
// GEMV. W8A16: the weight is stored e4m3 (1 byte) with a per-output-channel
// (row) absmax scale; the activation stays BF16. Halves the weight bytes read
// per decode step (the #1 decode cost, ~62% of a step — see
// tools/fp8_gemv_proto.cu, ~2x on large projections). Built alongside the
// BF16 weight (which the M>1 prefill/cuBLASLt path keeps using unchanged).
struct Fp8Shadow {
  uint8_t* w = nullptr;    // [N, K] e4m3
  float* scale = nullptr;  // [N] per-output-channel (absmax / 448)
  void Free();
};

// True if FP8 decode projections are enabled (Q4T_FP8_PROJ env, read once).
// When false, BuildFp8Shadow is a no-op (no alloc) and the decode path stays
// on Bf16Gev, so the feature is fully gated at zero cost.
bool Fp8ProjEnabled();

// Quantize a BF16 [N, K] weight into an e4m3 shadow (+ per-output-channel
// scale), allocating out->w and out->scale on the device. Returns true (with
// null fields) when !Fp8ProjEnabled(). Requires K % 16 == 0 (all qwen4_exp
// projection K's are multiples of 16). Returns false on a bad shape / alloc.
bool BuildFp8Shadow(const uint16_t* w_bf16, int N, int K, Fp8Shadow* out,
                    cudaStream_t stream);

// y[N] = alpha * (x[1, K] * dequant(w)^T), w = the FP8 shadow. beta must be 0.
// Returns false if unsupported (K % 16 != 0 or a null shadow); the caller then
// falls back to Bf16Gev / the cuBLASLt GEMM.
bool Fp8Gev(const uint16_t* x, const Fp8Shadow& w, uint16_t* y, int N, int K,
            float alpha, cudaStream_t stream);

}  // namespace model
}  // namespace q4t
