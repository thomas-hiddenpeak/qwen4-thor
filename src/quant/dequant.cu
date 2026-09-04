// NVFP4 -> BF16 dequantization kernel.
//
// Dequantizes packed e2m1 weights with their e4m3 group scales into BF16,
// applying the per-projection global scale:
//   W_real = e2m1_value * e4m3_group_scale / global_scale
// (global_scale = 1 / weight_scale_2 for the ModelOpt checkpoint).
//
// This is the W4A16 path: weights are dequantized to BF16 and multiplied
// against BF16 activations with a standard GEMM. The native W4A4 path
// (cuBLASLt FP4) keeps weights packed and quantizes activations at runtime
// instead (see act_quant.cu / fp4_gemm.h).
//
// Layout: `packed` is row-major [N, K/2] (2 e2m1 values per byte, low nibble
// = even index). `group_scale` is row-major [N, K/16] e4m3. `bf16_out` is
// row-major [N, K].
#include "q4t/quant/dequant.h"

#include <cuda_bf16.h>
#include <cuda_runtime.h>

#include "q4t/quant/format.h"

namespace q4t {
namespace quant {

namespace {

constexpr int kBlock = 256;

// One thread dequantizes one group of 16 e2m1 values (8 packed bytes).
__global__ void DequantFp4Kernel(const uint8_t* __restrict__ packed,
                                 const uint8_t* __restrict__ group_scale,
                                 uint16_t* __restrict__ bf16_out,
                                 float inv_global_scale, int N, int K) {
  const int groups = K / 16;
  const size_t total = static_cast<size_t>(N) * groups;
  const size_t idx = static_cast<size_t>(blockIdx.x) * blockDim.x + threadIdx.x;
  if (idx >= total) return;
  const int row = static_cast<int>(idx / groups);
  const int g = static_cast<int>(idx % groups);

  const uint8_t* p = packed + (static_cast<size_t>(row) * groups + g) * 8;
  const float gs = E4m3ToFloat(group_scale[static_cast<size_t>(row) * groups + g]);
  const float scale = gs * inv_global_scale;

  uint16_t* dst = bf16_out + (static_cast<size_t>(row) * K + g * 16);
#pragma unroll
  for (int j = 0; j < 8; ++j) {
    const uint8_t byte = p[j];
    const __nv_bfloat16 lo =
        __float2bfloat16_rn(E2m1ToFloat(byte & 0xF) * scale);
    const __nv_bfloat16 hi =
        __float2bfloat16_rn(E2m1ToFloat((byte >> 4) & 0xF) * scale);
    dst[2 * j] = *reinterpret_cast<const uint16_t*>(&lo);
    dst[2 * j + 1] = *reinterpret_cast<const uint16_t*>(&hi);
  }
}

}  // namespace

cudaError_t DequantFp4ToBf16Async(const uint8_t* packed,
                                  const uint8_t* group_scale,
                                  uint16_t* bf16_out, float inv_global_scale,
                                  int N, int K, cudaStream_t stream) {
  if (N == 0 || K == 0) return cudaSuccess;
  const size_t total = static_cast<size_t>(N) * (K / 16);
  const int blocks = static_cast<int>((total + kBlock - 1) / kBlock);
  DequantFp4Kernel<<<blocks, kBlock, 0, stream>>>(packed, group_scale, bf16_out,
                                                  inv_global_scale, N, K);
  return cudaGetLastError();
}

}  // namespace quant
}  // namespace q4t
