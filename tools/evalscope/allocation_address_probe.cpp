// Diagnostic LD_PRELOAD observer for the seven allocation/free APIs imported
// by the frozen main/HC executables. No device calls beyond exact forwarding.
// Addresses are virtual; this does not observe driver-internal allocations.
#include <cuda_runtime_api.h>
#include <dlfcn.h>
#include <sys/syscall.h>
#include <unistd.h>

#include <atomic>
#include <cerrno>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <ctime>

namespace {
std::atomic<uint64_t> sequence{0};
uint64_t Now() {
  timespec t{};
  if (clock_gettime(CLOCK_MONOTONIC, &t) != 0) std::abort();
  return uint64_t(t.tv_sec) * 1000000000 + t.tv_nsec;
}
template <typename Fn>
Fn Next(const char* name) {
  auto fn = reinterpret_cast<Fn>(dlsym(RTLD_NEXT, name));
  if (!fn) std::abort();
  return fn;
}
// Entry sequence and timestamps disambiguate concurrent completion order.
// Failed allocations never read the caller's output slot.
template <typename Fn, typename... Args>
cudaError_t Observe(const char* name, Fn real, int incoming_errno, void** slot,
                    void* freed, size_t bytes, cudaStream_t stream,
                    unsigned flags, Args... args) {
  const uint64_t id = sequence.fetch_add(1, std::memory_order_relaxed);
  const uint64_t begin = Now();
  errno = incoming_errno;
  const cudaError_t rc = real(args...);
  const int saved_errno = errno;
  const uint64_t end = Now();
  void* pointer = slot ? (rc == cudaSuccess ? *slot : nullptr) : freed;
  const long tid = syscall(SYS_gettid);
  const int written = std::fprintf(
      stderr,
      "[allocation-address] id=%llu tid=%ld api=%s begin_ns=%llu "
      "end_ns=%llu bytes=%zu ptr=%p stream=%p flags=%u rc=%d\n",
      static_cast<unsigned long long>(id), tid, name,
      static_cast<unsigned long long>(begin),
      static_cast<unsigned long long>(end), bytes, pointer,
      static_cast<void*>(stream), flags, static_cast<int>(rc));
  if (written < 0) std::abort();
  errno = saved_errno;
  return rc;
}
}  // namespace

extern "C" cudaError_t cudaMalloc(void** p, size_t n) {
  const int saved = errno;
  static auto real = Next<decltype(&cudaMalloc)>("cudaMalloc");
  return Observe("cudaMalloc", real, saved, p, nullptr, n, nullptr, 0, p, n);
}
extern "C" cudaError_t cudaMallocAsync(void** p, size_t n, cudaStream_t s) {
  const int saved = errno;
  static auto real = Next<decltype(&cudaMallocAsync)>("cudaMallocAsync");
  return Observe("cudaMallocAsync", real, saved, p, nullptr, n, s, 0, p, n, s);
}
extern "C" cudaError_t cudaMallocHost(void** p, size_t n) {
  const int saved = errno;
  static auto real = Next<decltype(&cudaMallocHost)>("cudaMallocHost");
  return Observe("cudaMallocHost", real, saved, p, nullptr, n, nullptr, 0, p, n);
}
extern "C" cudaError_t cudaHostAlloc(void** p, size_t n, unsigned flags) {
  const int saved = errno;
  static auto real = Next<decltype(&cudaHostAlloc)>("cudaHostAlloc");
  return Observe("cudaHostAlloc", real, saved, p, nullptr, n, nullptr, flags,
                 p, n, flags);
}
extern "C" cudaError_t cudaFree(void* p) {
  const int saved = errno;
  static auto real = Next<decltype(&cudaFree)>("cudaFree");
  return Observe("cudaFree", real, saved, nullptr, p, 0, nullptr, 0, p);
}
extern "C" cudaError_t cudaFreeAsync(void* p, cudaStream_t s) {
  const int saved = errno;
  static auto real = Next<decltype(&cudaFreeAsync)>("cudaFreeAsync");
  return Observe("cudaFreeAsync", real, saved, nullptr, p, 0, s, 0, p, s);
}
extern "C" cudaError_t cudaFreeHost(void* p) {
  const int saved = errno;
  static auto real = Next<decltype(&cudaFreeHost)>("cudaFreeHost");
  return Observe("cudaFreeHost", real, saved, nullptr, p, 0, nullptr, 0, p);
}
