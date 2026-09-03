// Tests for the PLE FP8 (e4m3) -> BF16 conversion kernel.
//
// The kernel is a pure byte->BF16 function, so we validate it against a CPU
// reference e4m3fn decoder over a set of crafted bytes (zero, subnormals,
// normals, negatives, max finite, NaN). Requires a CUDA device; skipped
// (reported as pass) when none is available.
#include "q4t/ple/fp8_convert.h"
#include "q4t/test.h"

#include <cuda_runtime.h>

#include <cmath>
#include <cstdint>
#include <cstring>
#include <vector>

namespace {

using q4t::ple::ConvertFp8ToBf16Async;

// CPU reference: decode one FP8 e4m3fn byte to float.
float DecodeE4M3(uint8_t b) {
  const int sign = (b >> 7) & 1;
  const int exp = (b >> 3) & 0xF;
  const int mant = b & 0x7;
  if (exp == 15 && mant == 7) return std::nan("");
  float val;
  if (exp == 0) {
    // Subnormal: 2^-6 * (mant / 8) = mant * 2^-9.
    val = static_cast<float>(std::ldexp(static_cast<double>(mant), -9));
  } else {
    val = static_cast<float>(
        std::ldexp(1.0 + static_cast<double>(mant) / 8.0, exp - 7));
  }
  return sign ? -val : val;
}

// BF16 is the top 16 bits of a float32.
float Bf16ToFloat(uint16_t b) {
  uint32_t bits = static_cast<uint32_t>(b) << 16;
  float f;
  std::memcpy(&f, &bits, sizeof(f));
  return f;
}

bool CudaAvailable() {
  int count = 0;
  if (cudaGetDeviceCount(&count) != cudaSuccess) return false;
  return count > 0;
}

}  // namespace

Q4T_TEST(ple_fp8_to_bf16_matches_reference) {
  if (!CudaAvailable()) {
    std::printf("  (skipped: no CUDA device)\n");
    return true;
  }

  // Crafted bytes covering the e4m3fn range.
  const std::vector<uint8_t> bytes = {
      0x00,  // +0
      0x01,  // subnormal 2^-9
      0x08,  // subnormal 2^-6
      0x20,  // 0.125
      0x38,  // 1.0
      0x40,  // 2.0
      0x50,  // 8.0
      0x5f,  // large finite
      0x7e,  // 448.0 (max finite)
      0x7f,  // NaN
      0x80,  // -0
      0xb8,  // -1.0
      0xff,  // -NaN
  };
  const size_t n = bytes.size();

  // Host inputs/outputs.
  std::vector<uint16_t> h_out(n, 0);
  uint8_t* d_in = nullptr;
  uint16_t* d_out = nullptr;
  Q4T_CHECK(cudaMalloc(&d_in, n) == cudaSuccess);
  Q4T_CHECK(cudaMalloc(&d_out, n * 2) == cudaSuccess);
  Q4T_CHECK(cudaMemcpy(d_in, bytes.data(), n, cudaMemcpyHostToDevice) ==
            cudaSuccess);

  cudaStream_t stream = nullptr;
  Q4T_CHECK(cudaStreamCreate(&stream) == cudaSuccess);
  Q4T_CHECK(ConvertFp8ToBf16Async(d_in, d_out, n, stream) == cudaSuccess);
  Q4T_CHECK(cudaStreamSynchronize(stream) == cudaSuccess);
  Q4T_CHECK(cudaMemcpy(h_out.data(), d_out, n * 2, cudaMemcpyDeviceToHost) ==
            cudaSuccess);

  // Compare against the CPU reference.
  for (size_t i = 0; i < n; ++i) {
    const float expected = DecodeE4M3(bytes[i]);
    const float got = Bf16ToFloat(h_out[i]);
    if (std::isnan(expected)) {
      if (!std::isnan(got)) {
        std::printf("  byte 0x%02x: expected NaN, got %f\n", bytes[i], got);
        Q4T_CHECK(false);
      }
    } else {
      // e4m3 finite values are exactly representable in BF16.
      if (expected != got) {
        std::printf("  byte 0x%02x: expected %f, got %f\n", bytes[i], expected,
                    got);
        Q4T_CHECK(false);
      }
    }
  }

  cudaFree(d_in);
  cudaFree(d_out);
  cudaStreamDestroy(stream);
  return true;
}

Q4T_TEST(ple_fp8_to_bf16_empty) {
  if (!CudaAvailable()) {
    std::printf("  (skipped: no CUDA device)\n");
    return true;
  }
  // count == 0 must be a no-op success.
  cudaStream_t stream = nullptr;
  Q4T_CHECK(cudaStreamCreate(&stream) == cudaSuccess);
  Q4T_CHECK(ConvertFp8ToBf16Async(nullptr, nullptr, 0, stream) == cudaSuccess);
  cudaStreamDestroy(stream);
  return true;
}
