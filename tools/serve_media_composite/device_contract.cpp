// Query the production AttentionKernel through the linked production adapter.
// No checkpoint load or kernel execution. Run only inside the stage GPU lease.
#include "q4t/vision/vision.h"

#include <algorithm>
#include <cstdio>
#include <cstdlib>

namespace {
int failure = 0, calls = 0, block_bytes = 0;
cudaFuncAttributes actual{};
void Check(bool ok, const char* why) {
  if (!ok) {
    std::fprintf(stderr, "FAIL %s\n", why);
    std::exit(1);
  }
}
}  // namespace
extern "C" cudaError_t __real_cudaGetDevice(int*);
extern "C" cudaError_t __real_cudaDeviceGetAttribute(int*, cudaDeviceAttr, int);
extern "C" cudaError_t __real_cudaFuncGetAttributes(cudaFuncAttributes*,
                                                  const void*);
extern "C" cudaError_t __wrap_cudaGetDevice(int* value) {
  ++calls;
  return failure == 1 ? cudaErrorInvalidDevice : __real_cudaGetDevice(value);
}
extern "C" cudaError_t __wrap_cudaDeviceGetAttribute(
    int* value, cudaDeviceAttr kind, int device) {
  ++calls;
  if (failure == 2) return cudaErrorInvalidValue;
  const auto result = __real_cudaDeviceGetAttribute(value, kind, device);
  if (result == cudaSuccess) {
    block_bytes = *value;
    if (failure == 4) *value = 0;
  }
  return result;
}
extern "C" cudaError_t __wrap_cudaFuncGetAttributes(cudaFuncAttributes* value,
                                                    const void* function) {
  ++calls;
  if (failure == 3) return cudaErrorInvalidValue;
  const auto result = __real_cudaFuncGetAttributes(value, function);
  if (result == cudaSuccess) {
    actual = *value;
    if (failure == 5) value->maxDynamicSharedSizeBytes = 0;
    if (failure == 6) value->maxThreadsPerBlock = 255;
    if (failure == 7) value->sharedSizeBytes = block_bytes;
  }
  return result;
}

int main() {
  uint64_t capacity = 0;
  Check(q4t::vision::VisionAttentionSharedBytes(&capacity) == cudaSuccess &&
            capacity > 0 && calls == 3 && actual.maxThreadsPerBlock >= 256,
        "actual capability query");
  const auto expected = std::min(
      static_cast<uint64_t>(block_bytes) - actual.sharedSizeBytes,
      static_cast<uint64_t>(actual.maxDynamicSharedSizeBytes));
  Check(capacity == expected && capacity >= 36864 && capacity < 65536,
        "actual device does not match frozen 1536/2048 boundary fixtures");
  const auto static_bytes = actual.sharedSizeBytes;
  const auto dynamic_bytes = actual.maxDynamicSharedSizeBytes;
  calls = 0;
  Check(q4t::vision::VisionAttentionSharedBytes(nullptr) ==
                cudaErrorInvalidValue &&
            !calls,
        "invalid output queried device");
  for (failure = 1; failure <= 7; ++failure) {
    uint64_t rejected = 123;
    Check(q4t::vision::VisionAttentionSharedBytes(&rejected) != cudaSuccess &&
              rejected == 0, "query failure published capacity");
  }
  std::printf("{\"passed\":true,\"attention_shared_bytes\":%lu,"
              "\"block_shared_bytes\":%d,\"static_shared_bytes\":%zu,"
              "\"function_dynamic_bytes\":%d,\"rejections\":8,"
              "\"kernel_executed\":false}\n", capacity, block_bytes,
              static_bytes, dynamic_bytes);
}
