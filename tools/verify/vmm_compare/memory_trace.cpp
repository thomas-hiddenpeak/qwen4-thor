// Independent research linkage only; production objects remain unchanged.
#include <cuda_runtime_api.h>
#include <dlfcn.h>
#include <sys/syscall.h>
#include <unistd.h>

#include <atomic>
#include <cstdio>
#include <ctime>
#include <map>
#include <mutex>

#ifdef Q4T_VMM_LINKED
#include "../vmm/block.h"
#endif

namespace {
std::atomic<unsigned long> calls{0};
std::mutex log_mutex;
unsigned long sequence = 0;
void Record(const char* api, const char* phase, unsigned long call, size_t bytes,
            const void* pointer, const void* stream, unsigned int flags,
            const void* caller, int result, const char* backend = "runtime") {
  std::lock_guard lock(log_mutex);
  timespec t{};
  clock_gettime(CLOCK_MONOTONIC, &t);
  Dl_info info{};
  dladdr(caller, &info);
  char line[768];
  int n = std::snprintf(line, sizeof(line),
      "[memory-trace] {\"seq\":%lu,\"call\":%lu,\"tid\":%ld,"
      "\"api\":\"%s\",\"phase\":\"%s\",\"bytes\":%zu,\"ptr\":%llu,"
      "\"stream\":%llu,\"flags\":%u,\"caller\":%llu,\"rc\":%d,"
      "\"mono\":%lld.%09ld,\"backend\":\"%s\"}\n",
      sequence++, call, syscall(SYS_gettid), api, phase, bytes,
      static_cast<unsigned long long>(reinterpret_cast<uintptr_t>(pointer)),
      static_cast<unsigned long long>(reinterpret_cast<uintptr_t>(stream)), flags,
      static_cast<unsigned long long>(reinterpret_cast<uintptr_t>(caller) -
                                     reinterpret_cast<uintptr_t>(info.dli_fbase)),
      result, static_cast<long long>(t.tv_sec), t.tv_nsec, backend);
  if (n <= 0 || n >= int(sizeof(line)) || write(2, line, n) != n) _exit(125);
}
#ifdef Q4T_VMM_LINKED
struct Registry {
  std::mutex mutex;
  std::map<void*, VmmBlock> blocks;
};
Registry& Blocks() {
  static auto* registry = new Registry;
  return *registry;
}
#endif
}  // namespace
extern "C" cudaError_t __real_cudaMalloc(void**, size_t);
extern "C" cudaError_t __real_cudaFree(void*);
extern "C" cudaError_t __real_cudaHostAlloc(void**, size_t, unsigned int);
extern "C" cudaError_t __real_cudaMallocHost(void**, size_t);
extern "C" cudaError_t __real_cudaFreeHost(void*);
extern "C" cudaError_t __real_cudaMallocAsync(void**, size_t, cudaStream_t);
extern "C" cudaError_t __real_cudaFreeAsync(void*, cudaStream_t);
extern "C" cudaError_t __real_cudaStreamSynchronize(cudaStream_t);
extern "C" cudaError_t __real_cudaDeviceSynchronize();
extern "C" cudaError_t __real_cudaEventRecord(cudaEvent_t, cudaStream_t);
extern "C" cudaError_t __real_cudaEventSynchronize(cudaEvent_t);

extern "C" cudaError_t __wrap_cudaMalloc(void** p, size_t bytes) {
  auto id = calls.fetch_add(1);
  void* caller = __builtin_return_address(0);
  const char* backend = "runtime";
#ifdef Q4T_VMM_LINKED
  if (bytes >= (32u << 20)) backend = "vmm";
#endif
  Record("cudaMalloc", "begin", id, bytes, nullptr, nullptr, 0, caller, -1, backend);
  cudaError_t rc;
#ifdef Q4T_VMM_LINKED
  if (bytes >= (32u << 20)) {
    rc = __real_cudaFree(nullptr);  // Establish runtime primary context.
    int device = -1;
    if (rc == cudaSuccess) rc = cudaGetDevice(&device);
    if (rc == cudaSuccess) {
      VmmBlock block;
      CUresult result = block.Allocate(bytes, device);
      if (result == CUDA_SUCCESS) {
        *p = reinterpret_cast<void*>(block.address);
        auto& registry = Blocks();
        std::lock_guard lock(registry.mutex);
        if (!registry.blocks.emplace(*p, block).second) std::abort();
      } else {
        // Never silently drop retained resources after failed rollback.
        if (block.address) std::abort();
        rc = result == CUDA_ERROR_OUT_OF_MEMORY ? cudaErrorMemoryAllocation
                                                : cudaErrorUnknown;
      }
    }
  } else
#endif
  rc = __real_cudaMalloc(p, bytes);
  Record("cudaMalloc", "end", id, bytes, rc == cudaSuccess ? *p : nullptr,
         nullptr, 0, caller, int(rc), backend);
  return rc;
}

extern "C" cudaError_t __wrap_cudaFree(void* p) {
  auto id = calls.fetch_add(1);
  void* caller = __builtin_return_address(0);
  const char* backend = "runtime";
#ifdef Q4T_VMM_LINKED
  auto& registry = Blocks();
  std::unique_lock lock(registry.mutex);
  auto it = registry.blocks.find(p);
  if (it != registry.blocks.end()) backend = "vmm";
#endif
  Record("cudaFree", "begin", id, 0, p, nullptr, 0, caller, -1, backend);
  cudaError_t rc;
#ifdef Q4T_VMM_LINKED
  if (it != registry.blocks.end()) {
    // cuMemUnmap does not supply cudaFree's implicit completion behavior.
    rc = __real_cudaDeviceSynchronize();
    if (rc == cudaSuccess) {
      auto result = it->second.Free();
      rc = result == CUDA_SUCCESS ? cudaSuccess : cudaErrorUnknown;
      if (rc == cudaSuccess) registry.blocks.erase(it);
    }
  } else {
    lock.unlock();
    rc = __real_cudaFree(p);
  }
#else
  rc = __real_cudaFree(p);
#endif
  Record("cudaFree", "end", id, 0, p, nullptr, 0, caller, int(rc), backend);
  return rc;
}

#define TRACE_ALLOC(api, signature, arguments, stream_arg, flags_arg) \
  extern "C" cudaError_t __wrap_##api signature { \
    auto id = calls.fetch_add(1); \
    void* caller = __builtin_return_address(0); \
    Record(#api, "begin", id, bytes, nullptr, stream_arg, flags_arg, caller, -1); \
    auto rc = __real_##api arguments; \
    Record(#api, "end", id, bytes, rc == cudaSuccess ? *p : nullptr, \
           stream_arg, flags_arg, caller, int(rc)); \
    return rc; \
  }
TRACE_ALLOC(cudaHostAlloc, (void** p, size_t bytes, unsigned int flags),
            (p, bytes, flags), nullptr, flags)
TRACE_ALLOC(cudaMallocHost, (void** p, size_t bytes), (p, bytes), nullptr, 0)
TRACE_ALLOC(cudaMallocAsync, (void** p, size_t bytes, cudaStream_t stream),
            (p, bytes, stream), stream, 0)

#define TRACE_CALL(api, signature, arguments, ptr_arg, stream_arg) \
  extern "C" cudaError_t __wrap_##api signature { \
    auto id = calls.fetch_add(1); \
    void* caller = __builtin_return_address(0); \
    Record(#api, "begin", id, 0, ptr_arg, stream_arg, 0, caller, -1); \
    auto rc = __real_##api arguments; \
    Record(#api, "end", id, 0, ptr_arg, stream_arg, 0, caller, int(rc)); \
    return rc; \
  }
TRACE_CALL(cudaFreeHost, (void* p), (p), p, nullptr)
extern "C" cudaError_t __wrap_cudaFreeAsync(void* p, cudaStream_t stream) {
#ifdef Q4T_VMM_LINKED
  {
    auto& registry = Blocks();
    std::lock_guard lock(registry.mutex);
    // This prototype changes only the synchronous malloc/free ownership pair.
    if (registry.blocks.contains(p)) std::abort();
  }
#endif
  auto id = calls.fetch_add(1);
  void* caller = __builtin_return_address(0);
  Record("cudaFreeAsync", "begin", id, 0, p, stream, 0, caller, -1);
  auto rc = __real_cudaFreeAsync(p, stream);
  Record("cudaFreeAsync", "end", id, 0, p, stream, 0, caller, int(rc));
  return rc;
}
TRACE_CALL(cudaStreamSynchronize, (cudaStream_t stream), (stream), nullptr, stream)
TRACE_CALL(cudaDeviceSynchronize, (), (), nullptr, nullptr)
TRACE_CALL(cudaEventRecord, (cudaEvent_t event, cudaStream_t stream),
           (event, stream), event, stream)
TRACE_CALL(cudaEventSynchronize, (cudaEvent_t event), (event), event, nullptr)
