// BF16 -> NVFP4 activation quantization kernel (W4A4 path).
#pragma once

#include <cuda_runtime.h>

#include <cstdint>

namespace q4t {
namespace quant {

// Quantize a BF16 activation tensor [M, K] to NVFP4 at runtime.
//
//   bf16       : row-major [M, K] uint16 (BF16), input
//   packed_out : row-major [M, K/2] uint8 (2 e2m1 / byte), caller-allocated
//   sf_out     : swizzled e4m3 scale factors, caller-allocated with at least
//                SfBufferSize(M, K) bytes (see swizzle.h)
//
// Per 16-element group along K: group_scale = gmax / 6.0, e4m3-rounded; e2m1
// codes are rounded against the e4m3-rounded scale so the result matches the
// hardware bit-for-bit. The FP32 global activation scale is NOT applied here;
// fold it into the GEMM alpha (see fp4_gemm.h).
//
// K must be a multiple of 16. Launches on `stream`. Returns cudaSuccess.
cudaError_t QuantizeActivationToFp4Async(const uint16_t* bf16,
                                         uint8_t* packed_out,
                                         uint8_t* sf_out, int M, int K,
                                         cudaStream_t stream);

}  // namespace quant
}  // namespace q4t
