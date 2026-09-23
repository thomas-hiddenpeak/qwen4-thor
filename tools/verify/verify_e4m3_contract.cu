// Independent E4M3 contract audit. Run only after HTTP E2E gate review.
// See docs/QUANT_REFERENCE_CONTRACT_2026-09-23.md for scope and usage.
#include <cuda_fp8.h>
#include <cuda_runtime.h>

#include <cmath>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <limits>
#include <vector>

#include "q4t/quant/format.h"

struct Case {
  float value;
  uint8_t expected;
};

__global__ void Encode(const Case* cases, uint8_t* actual, uint8_t* native,
                       int count) {
  const int i = blockIdx.x * blockDim.x + threadIdx.x;
  if (i >= count) return;
  actual[i] = q4t::quant::FloatToE4m3(cases[i].value);
  native[i] = __nv_cvt_float_to_fp8(cases[i].value, __NV_SATFINITE, __NV_E4M3);
}

bool Check(cudaError_t status) {
  if (status == cudaSuccess) return true;
  std::fprintf(stderr, "%s\n", cudaGetErrorString(status));
  return false;
}

int main(int argc, char** argv) {
  if (argc != 2) return 2;
  FILE* report = std::fopen(argv[1], "w");
  if (!report) return 2;
  // Build format values from the independent exponent/mantissa definition.
  // Neither expected rounding nor reconstruction calls q4t converters.
  std::vector<float> values;
  for (int code = 0; code < 127; ++code) {
    const int exp = code / 8, mant = code % 8;
    values.push_back(exp == 0 ? std::ldexp(static_cast<float>(mant), -9)
                             : std::ldexp(1.0f + mant / 8.0f, exp - 7));
  }
  std::vector<Case> cases;
  for (int code = 0; code < 127; ++code) {
    cases.push_back({values[code], static_cast<uint8_t>(code)});
  }
  for (int code = 0; code < 126; ++code) {
    const float middle = (values[code] + values[code + 1]) * 0.5f;
    cases.push_back({std::nextafter(middle, 0.0f),
                     static_cast<uint8_t>(code)});
    cases.push_back({middle, static_cast<uint8_t>((code & 1) ? code + 1 : code)});
    cases.push_back({std::nextafter(middle, 1000.0f),
                     static_cast<uint8_t>(code + 1)});
  }
  cases.push_back({std::numeric_limits<float>::max(), 126});
  // Independent nearest-value search over every finite nonnegative BF16.
  for (uint32_t bits = 0; bits < 0x7f80; ++bits) {
    const uint32_t full = bits << 16;
    float value;
    std::memcpy(&value, &full, sizeof(value));
    int nearest = 126;
    if (value < values.back()) {
      double distance = std::numeric_limits<double>::infinity();
      for (int code = 0; code < 127; ++code) {
        const double delta = std::abs(static_cast<double>(value) - values[code]);
        if (delta < distance || (delta == distance && !(code & 1))) {
          nearest = code;
          distance = delta;
        }
      }
    }
    cases.push_back({value, static_cast<uint8_t>(nearest)});
  }
  const int count = static_cast<int>(cases.size());
  Case* device_cases = nullptr;
  uint8_t *device_actual = nullptr, *device_native = nullptr;
  if (!Check(cudaMalloc(&device_cases, cases.size() * sizeof(Case))) ||
      !Check(cudaMalloc(&device_actual, cases.size())) ||
      !Check(cudaMalloc(&device_native, cases.size()))) return 2;
  if (!Check(cudaMemcpy(device_cases, cases.data(), cases.size() * sizeof(Case),
                        cudaMemcpyHostToDevice))) return 2;
  Encode<<<(count + 255) / 256, 256>>>(device_cases, device_actual, device_native,
                                     count);
  if (!Check(cudaGetLastError()) || !Check(cudaDeviceSynchronize())) return 2;
  std::vector<uint8_t> actual(count), native(count);
  if (!Check(cudaMemcpy(actual.data(), device_actual, cases.size(),
                        cudaMemcpyDeviceToHost)) ||
      !Check(cudaMemcpy(native.data(), device_native, cases.size(),
                        cudaMemcpyDeviceToHost))) return 2;
  int host_bad = 0, device_bad = 0, native_bad = 0;
  for (int i = 0; i < count; ++i) {
    const Case& c = cases[i];
    const auto host = q4t::quant::FloatToE4m3(c.value);
    std::fprintf(report, "%a %u %u %u %u\n", c.value, c.expected, host,
                 actual[i], native[i]);
    host_bad += host != c.expected;
    device_bad += actual[i] != c.expected;
    native_bad += native[i] != c.expected;
    if ((host != c.expected || actual[i] != c.expected || native[i] != c.expected)
        && host_bad + device_bad + native_bad <= 30) {
      std::printf("value=%a expected=%u host=%u device=%u native=%u\n",
                  c.value, c.expected, host, actual[i], native[i]);
    }
  }
  if (!Check(cudaFree(device_native)) || !Check(cudaFree(device_actual)) ||
      !Check(cudaFree(device_cases))) return 2;
  if (std::fclose(report) != 0) return 2;
  std::printf("cases=%d host_bad=%d device_bad=%d native_bad=%d\n", count,
              host_bad, device_bad, native_bad);
  return host_bad || device_bad || native_bad ? 1 : 0;
}
