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
//   global_scale : the FP32 activation global scale (= checkpoint input_scale,
//                > 0). The per-group e4m3 is round(block_scale / global_scale)
//                so that the GEMM (which multiplies by global_scale via alpha,
//                see fp4_gemm.h) reconstructs the activation:
//                    a_recon = e2m1 * e4m3 * global_scale ~= a
//                This mirrors how the checkpoint stores weights (e4m3 =
//                block_scale / weight_scale_2).
//
// Per 16-element group along K: block_scale = gmax / 6.0 (6.0 = max e2m1
// magnitude); e4m3 = round(block_scale / global_scale); e2m1 codes are rounded
// against (e4m3 * global_scale) so the result matches the hardware bit-for-bit.
//
// K must be a multiple of 16. Launches on `stream`. Returns cudaSuccess.
cudaError_t QuantizeActivationToFp4Async(const uint16_t* bf16,
                                         uint8_t* packed_out,
                                         uint8_t* sf_out, int M, int K,
                                         float global_scale,
                                         cudaStream_t stream);

}  // namespace quant
}  // namespace q4t
