// PLE FP8 (e4m3) -> BF16 conversion kernel.
//
// The PLE SSD sidecar stores each 160-byte row as FP8 e4m3. After the io_uring
// reader scatters rows into a pinned staging buffer, this kernel converts the
// FP8 bytes to BF16 on a side CUDA stream. It performs a pure type conversion
// (no scaling); the per-table `weight_scale` is applied later in the PLE layer
// forward (after the 16-head reduce), matching the SGLang reference:
//   embeddings = reduce(fa8_to_bf16(rows)) * weight_scale
#pragma once

#include <cuda_runtime.h>

#include <cstdint>
#include <cstddef>

namespace q4t {
namespace ple {

// Convert `count` FP8 e4m3 values (contiguous bytes) to BF16.
// `fp8` must be 2-byte aligned; `bf16_out` must have room for 2*count bytes.
// Launch on `stream`. Returns cudaSuccess on success.
cudaError_t ConvertFp8ToBf16Async(const uint8_t* fp8, uint16_t* bf16_out,
                                  size_t count, cudaStream_t stream);

}  // namespace ple
}  // namespace q4t
