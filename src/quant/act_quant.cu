// BF16 -> NVFP4 activation quantization kernel (W4A4 path).
//
// Quantizes a BF16 activation tensor [M, K] to NVFP4 at runtime so it can be
// multiplied against packed NVFP4 weights with the native cuBLASLt FP4 GEMM.
//
// For each group of 16 elements along K:
//   gmax        = max |a| in the group
//   group_scale = gmax / 6.0            (6.0 = max e2m1 magnitude)
//   e4m3_sf     = round_to_even(group_scale)
//   e2m1_code   = round_to_even(a / group_scale)
//
// The per-group e4m3 scale factors are written in the swizzled layout that
// cuBLASLt CUBLASLT_MATMUL_MATRIX_SCALE_VEC16_UE4M3 expects (see swizzle.h),
// and the FP4 payload is written row-major [M, K/2]. The FP32 global
// activation scale (1 / input_scale) is folded into the GEMM alpha by the
// caller, not applied here.
#include "q4t/quant/act_quant.h"

#include <cuda_bf16.h>
#include <cuda_runtime.h>

#include "q4t/quant/format.h"
#include "q4t/quant/swizzle.h"

namespace q4t {
namespace quant {

namespace {

constexpr int kBlock = 256;

// Device copy of the SF swizzle offset (swizzle.h is host-only).
__device__ __forceinline__ size_t SfOffsetDev(int row, int group,
                                              int num_g_tiles) {
  const int i = row % 32;
  const int j = (row % 128) / 32;
  const int ga = group % 4;
  const int within = i * 16 + j * 4 + ga;
  return static_cast<size_t>(within) +
         static_cast<size_t>(group / 4) * 512 +
         static_cast<size_t>(row / 128) * static_cast<size_t>(num_g_tiles) *
             512;
}

// One thread quantizes one group of 16 BF16 values.
__global__ void ActQuantKernel(const uint16_t* __restrict__ bf16,
                               uint8_t* __restrict__ packed_out,
                               uint8_t* __restrict__ sf_out, int M, int K,
                               int num_g_tiles) {
  const int groups = K / 16;
  const size_t total = static_cast<size_t>(M) * groups;
  const size_t idx = static_cast<size_t>(blockIdx.x) * blockDim.x + threadIdx.x;
  if (idx >= total) return;
  const int row = static_cast<int>(idx / groups);
  const int g = static_cast<int>(idx % groups);

  const uint16_t* src = bf16 + (static_cast<size_t>(row) * K + g * 16);
  float a[16];
  float gmax = 0.0f;
#pragma unroll
  for (int j = 0; j < 16; ++j) {
    const __nv_bfloat16 b = *reinterpret_cast<const __nv_bfloat16*>(&src[j]);
    a[j] = __bfloat162float(b);
    gmax = fmaxf(gmax, fabsf(a[j]));
  }
  const float group_scale = gmax > 0.0f ? gmax / 6.0f : 1.0f;
  // The hardware only sees the e4m3-rounded scale, so the e2m1 codes must be
  // computed against the rounded value (not the float) to match cuBLASLt
  // bit-for-bit.
  const uint8_t sf_code = FloatToE4m3(group_scale);
  const float sf_float = E4m3ToFloat(sf_code);
  const float inv = sf_float > 0.0f ? 1.0f / sf_float : 0.0f;

  // Pack 16 e2m1 codes into 8 bytes (low nibble = even index).
  uint8_t bytes[8];
#pragma unroll
  for (int j = 0; j < 8; ++j) {
    const int c0 = FloatToE2m1Code(a[2 * j] * inv);
    const int c1 = FloatToE2m1Code(a[2 * j + 1] * inv);
    bytes[j] = static_cast<uint8_t>((c1 << 4) | (c0 & 0xF));
  }
  uint8_t* dst = packed_out + (static_cast<size_t>(row) * groups + g) * 8;
#pragma unroll
  for (int j = 0; j < 8; ++j) dst[j] = bytes[j];

  // Swizzled e4m3 scale factor.
  sf_out[SfOffsetDev(row, g, num_g_tiles)] = sf_code;
}

}  // namespace

cudaError_t QuantizeActivationToFp4Async(const uint16_t* bf16,
                                         uint8_t* packed_out,
                                         uint8_t* sf_out, int M, int K,
                                         cudaStream_t stream) {
  if (M == 0 || K == 0) return cudaSuccess;
  const size_t total = static_cast<size_t>(M) * (K / 16);
  const int blocks = static_cast<int>((total + kBlock - 1) / kBlock);
  const int num_g_tiles = SfNumGtiles(K);
  ActQuantKernel<<<blocks, kBlock, 0, stream>>>(bf16, packed_out, sf_out, M, K,
                                                num_g_tiles);
  return cudaGetLastError();
}

}  // namespace quant
}  // namespace q4t
