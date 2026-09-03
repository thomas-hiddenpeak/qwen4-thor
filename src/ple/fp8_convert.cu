// PLE FP8 (e4m3) -> BF16 conversion kernel implementation.
#include "q4t/ple/fp8_convert.h"

#include <cuda_bf16.h>
#include <cuda_fp8.h>
#include <cuda_runtime.h>

namespace q4t {
namespace ple {

namespace {

constexpr int kBlock = 256;

// One thread converts one FP8 byte to a BF16 value.
__global__ void Fp8ToBf16Kernel(const uint8_t* __restrict__ fp8,
                                uint16_t* __restrict__ bf16_out,
                                size_t count) {
  const size_t i = static_cast<size_t>(blockIdx.x) * blockDim.x + threadIdx.x;
  if (i >= count) return;
  const __half_raw hraw = __nv_cvt_fp8_to_halfraw(
      static_cast<__nv_fp8_storage_t>(fp8[i]), __NV_E4M3);
  const __half h(hraw);
  const __nv_bfloat16 bf = __float2bfloat16(__half2float(h));
  bf16_out[i] = *reinterpret_cast<const uint16_t*>(&bf);
}

}  // namespace

cudaError_t ConvertFp8ToBf16Async(const uint8_t* fp8, uint16_t* bf16_out,
                                  size_t count, cudaStream_t stream) {
  if (count == 0) return cudaSuccess;
  const int blocks = static_cast<int>((count + kBlock - 1) / kBlock);
  Fp8ToBf16Kernel<<<blocks, kBlock, 0, stream>>>(fp8, bf16_out, count);
  return cudaGetLastError();
}

}  // namespace ple
}  // namespace q4t
