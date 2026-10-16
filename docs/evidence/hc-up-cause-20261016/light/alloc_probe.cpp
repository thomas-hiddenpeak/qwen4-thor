#include <cuda_runtime_api.h>
#include <dlfcn.h>
#include <time.h>
#include <cstdio>
#include <cstdlib>

namespace {
long long Now() {
  timespec t{};
  clock_gettime(CLOCK_MONOTONIC, &t);
  return static_cast<long long>(t.tv_sec) * 1000000000 + t.tv_nsec;
}
}
extern "C" cudaError_t cudaMallocAsync(void** p, size_t n, cudaStream_t s) {
  using Fn = cudaError_t (*)(void**, size_t, cudaStream_t);
  static Fn real = reinterpret_cast<Fn>(dlsym(RTLD_NEXT, "cudaMallocAsync"));
  if (!real) std::abort();
  const long long start = Now();
  const cudaError_t result = real(p, n, s);
  const long long end = Now();
  // Log every allocation: no threshold-based selection of observations.
  std::fprintf(stderr, "[alloc-probe] start_ns=%lld duration_ns=%lld bytes=%zu rc=%d\n",
               start, end - start, n, static_cast<int>(result));
  return result;
}
