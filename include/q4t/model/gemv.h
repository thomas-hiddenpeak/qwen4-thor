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

// FP8 decode is gated per projection group so each group's speed and quality
// can be measured independently. Q4T_FP8_PROJ enables all groups; each group
// also has its own env var (below) to enable just that group.
enum class Fp8Part { kAttn, kGdn, kLmHead, kMoeShared };

// True if FP8 decode is enabled for `part`: Q4T_FP8_PROJ (all groups) or the
// group's own env — Q4T_FP8_ATTN / _GDN / _LMHEAD / _SHARED. A value of "0" or
// empty counts as off. Read once.
bool Fp8ProjEnabled(Fp8Part part);

// Unconditionally quantize a BF16 [N, K] weight into an e4m3 shadow (+ per-
// output-channel absmax/448 scale), allocating out->w and out->scale on the
// device. Requires K % 16 == 0. Returns false on a bad shape / alloc. Used by
// BuildFp8Shadow and by tests that must execute the FP8 GEMV path regardless
// of the env gate.
bool QuantizeToFp8Shadow(const uint16_t* w_bf16, int N, int K, Fp8Shadow* out,
                         cudaStream_t stream);

// Gated shadow build: quantizes (via QuantizeToFp8Shadow) only when
// Fp8ProjEnabled(part); otherwise leaves the shadow null so the decode path
// stays on Bf16Gev at zero cost.
bool BuildFp8Shadow(const uint16_t* w_bf16, int N, int K, Fp8Shadow* out,
                    Fp8Part part, cudaStream_t stream);

// y[N] = alpha * (x[1, K] * dequant(w)^T), w = the FP8 shadow. beta must be 0.
// Returns false if unsupported (K % 16 != 0 or a null shadow); the caller then
// falls back to Bf16Gev / the cuBLASLt GEMM.
bool Fp8Gev(const uint16_t* x, const Fp8Shadow& w, uint16_t* y, int N, int K,
            float alpha, cudaStream_t stream);

}  // namespace model
}  // namespace q4t
