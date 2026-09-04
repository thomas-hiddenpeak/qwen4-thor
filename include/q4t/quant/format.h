// NVFP4 (FP4 e2m1 + FP8 e4m3 group scale) format primitives.
//
// The qwen4_exp checkpoint stores routed-expert weights as NVFP4:
//   *.weight         : uint8, e2m1 4-bit packed (2 values / byte)
//   *.weight_scale   : FP8 e4m3, one per 16-element group along K
//   *.weight_scale_2 : FP32 scalar (ModelOpt inv_global_scale = 1/global)
//   *.input_scale    : FP32 scalar (activation global scale, W4A4 only)
//
// Dequantization (matches qwen35-thor / llm-compressor convention):
//   W_real = e2m1_value * e4m3_group_scale / global_scale
// where global_scale = 1 / weight_scale_2 (ModelOpt stores the inverse).
//
// This header is shared by host code, device kernels, and tests. The
// converters are table-free (computed from the bits) so they are valid in
// device code, and they use round-to-nearest-even, matching the checkpoint
// quantizer (CUTLASS float_e2m1_t / float_ue4m3_t).
#pragma once

#include <cmath>
#include <cstdint>

// Allow host-only (non-CUDA) translation units to include this header.
#ifndef __host__
#define __host__
#endif
#ifndef __device__
#define __device__
#endif

namespace q4t {
namespace quant {

// Decode an e2m1 (NVFP4) 4-bit code to float.
//
// e2m1: 1 sign bit, 2 exponent bits (bias 1), 1 mantissa bit. Codes 0..7 are
// non-negative, 8..15 their negations. Magnitudes: 0, 0.5, 1, 1.5, 2, 3, 4, 6.
// Computed from the bits (no table) so it is callable from device code:
//   exp_field = (code >> 1) & 3, mant = code & 1
//     exp_field == 0 : subnormal, value = mant * 0.5
//     exp_field > 0  : normal, value = (1 + mant/2) * 2^(exp_field - 1)
inline __host__ __device__ float E2m1ToFloat(uint8_t code) {
  const int exp_field = (code >> 1) & 0x3;
  const int mant = code & 0x1;
  float v;
  if (exp_field == 0) {
    v = static_cast<float>(mant) * 0.5f;
  } else {
    v = (1.0f + mant * 0.5f) * ldexpf(1.0f, exp_field - 1);
  }
  return (code & 0x8) ? -v : v;
}

// Round a float to the nearest e2m1 value (round-to-nearest-even) and return
// the 4-bit code. Ties (exactly halfway between two representable values) go
// to the code whose mantissa bit is even, matching CUTLASS float_e2m1_t.
//
// e2m1 representable magnitudes: 0, 0.5, 1, 1.5, 2, 3, 4, 6. The midpoints
// between consecutive values are 0.25, 0.75, 1.25, 1.75, 2.5, 3.5, 5.0.
//   0.25  -> 0.0  (code 0, even)  [tie between 0 and 0.5]
//   0.75  -> 1.0  (code 2, even)  [tie between 0.5 and 1.0]
//   1.25  -> 1.0  (code 2, even)  [tie between 1.0 and 1.5]
//   1.75  -> 2.0  (code 4, even)  [tie between 1.5 and 2.0]
//   2.5   -> 2.0  (code 4, even)  [tie between 2.0 and 3.0]
//   3.5   -> 4.0  (code 6, even)  [tie between 3.0 and 4.0]
//   5.0   -> 4.0  (code 6, even)  [tie between 4.0 and 6.0]
inline __host__ __device__ int FloatToE2m1Code(float v) {
  const float a = v < 0.0f ? -v : v;
  int code = 0;
  // Round-to-nearest-even: ties (exact midpoints) go to the code whose
  // mantissa bit is even (codes 0, 2, 4, 6). Hence `<=` at the midpoints
  // 0.25 / 1.25 / 2.5 / 5.0 and `<` at 0.75 / 1.75 / 3.5.
  if (a <= 0.25f) {
    code = 0;  // 0.0
  } else if (a < 0.75f) {
    code = 1;  // 0.5
  } else if (a <= 1.25f) {
    code = 2;  // 1.0
  } else if (a < 1.75f) {
    code = 3;  // 1.5
  } else if (a <= 2.5f) {
    code = 4;  // 2.0
  } else if (a < 3.5f) {
    code = 5;  // 3.0
  } else if (a <= 5.0f) {
    code = 6;  // 4.0
  } else {
    code = 7;  // 6.0 (saturate)
  }
  return v < 0.0f && v != -0.0f ? (code | 8) : code;
}

// Decode an unsigned e4m3 (UE4M3) 8-bit code to float.
// UE4M3 (the format cuBLASLt CUBLASLT_MATMUL_MATRIX_SCALE_VEC16_UE4M3 uses):
// 4-bit exponent (bias 7), 3-bit mantissa, NO sign bit. For positive values
// this is bit-identical to the signed e4m3fn layout, which is how the
// checkpoint's F8_E4M3 group scales are stored:
//   code 0x00     : 0.0
//   code 0x01..0x07 : subnormal, value = mantissa * 2^-9
//   code 0x08..0x7E : normal, value = (1 + mantissa/8) * 2^(exp-7)
//   code 0x7F     : NaN (decoded as 0.0 for safety)
// The maximum finite value is 0x7E = (1 + 6/8) * 2^8 = 448.0. Computed from
// the bits (no table) so it is callable from device code.
inline __host__ __device__ float E4m3ToFloat(uint8_t code) {
  const int exp = code >> 3;  // 0..15
  const int man = code & 0x7;
  if (exp == 0) {
    return static_cast<float>(man) * 0.001953125f;  // man * 2^-9
  }
  if (exp == 15 && man == 7) {
    return 0.0f;  // 0x7F = NaN (guard)
  }
  const float mant = 1.0f + man * 0.125f;
  return ldexpf(mant, exp - 7);
}

// Round a non-negative float to the nearest unsigned e4m3 value
// (round-to-nearest-even) and return the 8-bit code. Scales are always
// non-negative in NVFP4 (they are absolute magnitudes). Values above the
// UE4M3 max (448.0) saturate to the largest normal code 0x7E.
inline __host__ __device__ uint8_t FloatToE4m3(float v) {
  float a = v < 0.0f ? -v : v;
  if (a == 0.0f) return 0;
  // Subnormal range: [0, 2^-6). The subnormal step is 2^-9.
  if (a < 0.015625f) {  // < 2^-6
    int man = static_cast<int>(a * 512.0f + 0.5f);
    if (man > 7) man = 7;
    return static_cast<uint8_t>(man);
  }
  // Normal range: a >= 2^-6. Find e = floor(log2(a)) so that m = a / 2^e
  // lies in [1, 2). UE4M3 normal: value = (1 + man/8) * 2^(exp-7), exp in
  // [1, 15], i.e. e in [-6, 8].
  int e = 0;
  float m = a;
  while (m >= 2.0f) {
    m *= 0.5f;
    e++;
  }
  while (m < 1.0f) {
    m *= 2.0f;
    e--;
  }
  // m in [1, 2). The 3-bit mantissa holds (m - 1) * 8 in [0, 8). Use
  // round-to-nearest-even on the dropped bit.
  const float frac = (m - 1.0f) * 8.0f;  // in [0, 8)
  int base = static_cast<int>(frac);  // 0..7 (floor)
  const float rem = frac - base;
  int man = base;
  if (rem > 0.5f || (rem == 0.5f && (base & 1))) {
    man = base + 1;  // may become 8 -> carry
  }
  int exp = e + 7;
  if (man == 8) {
    man = 0;
    exp++;
  }
  if (exp > 14) {
    return 0x7E;  // saturate to max normal (448.0)
  }
  if (exp < 1) {
    // Carried back into subnormal (only at the 2^-6 boundary).
    int sm = static_cast<int>(a * 512.0f + 0.5f);
    if (sm > 7) sm = 7;
    return static_cast<uint8_t>(sm);
  }
  return static_cast<uint8_t>((exp << 3) | man);
}

}  // namespace quant
}  // namespace q4t
