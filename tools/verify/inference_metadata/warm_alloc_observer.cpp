// Diagnostic-only linker wrappers. Never linked into the default executable.
#include <cuda_runtime_api.h>
#include <dlfcn.h>
#include <unistd.h>

#include <atomic>
#include <cstdio>
#include <ctime>

namespace {
std::atomic<unsigned long> sequence{0};
void Record(const char* api, const char* phase, unsigned long id, size_t bytes,
            void* caller, int result) {
  timespec now{};
  clock_gettime(CLOCK_MONOTONIC, &now);
  Dl_info info{};
  dladdr(caller, &info);
  char line[512];
  int size = std::snprintf(
      line, sizeof(line),
      "[alloc-observer] %s %s id=%lu bytes=%zu mono=%lld.%09ld "
      "caller_offset=%llu result=%d\n", api, phase, id, bytes,
      static_cast<long long>(now.tv_sec), now.tv_nsec,
      static_cast<unsigned long long>(reinterpret_cast<uintptr_t>(caller) -
                                     reinterpret_cast<uintptr_t>(info.dli_fbase)),
      result);
  if (size > 0 && size < static_cast<int>(sizeof(line))) {
    const auto written = write(STDERR_FILENO, line, size);
    if (written != size) _exit(125);
  }
}
}  // namespace

extern "C" cudaError_t __real_cudaMalloc(void**, size_t);
extern "C" cudaError_t __real_cudaMallocHost(void**, size_t);
extern "C" cudaError_t __real_cudaHostAlloc(void**, size_t, unsigned int);

#define OBSERVE_ALLOC(api, signature, arguments)                       \
  extern "C" cudaError_t __wrap_##api signature {                     \
    const auto id = sequence.fetch_add(1);                            \
    void* caller = __builtin_return_address(0);                       \
    Record(#api, "begin", id, bytes, caller, -1);                    \
    auto result = __real_##api arguments;                             \
    Record(#api, "end", id, bytes, caller, static_cast<int>(result)); \
    return result;                                                   \
  }

OBSERVE_ALLOC(cudaMalloc, (void** p, size_t bytes), (p, bytes))
OBSERVE_ALLOC(cudaMallocHost, (void** p, size_t bytes), (p, bytes))
OBSERVE_ALLOC(cudaHostAlloc, (void** p, size_t bytes, unsigned int flags),
              (p, bytes, flags))
