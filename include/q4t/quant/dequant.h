// NVFP4 -> BF16 dequantization kernel (W4A16 path).
#pragma once

#include <cuda_runtime.h>

#include <cstdint>

namespace q4t {
namespace quant {

// Dequantize packed NVFP4 weights to BF16, applying the per-projection
// global scale.
//
//   packed       : row-major [N, K/2] uint8 (2 e2m1 / byte, low nibble = even)
//   group_scale  : row-major [N, K/16] uint8 (e4m3)
//   bf16_out     : row-major [N, K] uint16 (BF16), caller-allocated
//   inv_global_scale : 1 / global_scale (global_scale = 1 / weight_scale_2)
//
// W_real[i, k] = e2m1(packed) * e4m3(group_scale) * inv_global_scale
//
// K must be a multiple of 16. Launches on `stream`. Returns cudaSuccess.
cudaError_t DequantFp4ToBf16Async(const uint8_t* packed,
                                  const uint8_t* group_scale,
                                  uint16_t* bf16_out, float inv_global_scale,
                                  int N, int K, cudaStream_t stream);

}  // namespace quant
}  // namespace q4t
