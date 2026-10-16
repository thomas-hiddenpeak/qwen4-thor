// Diagnostic-only interposition; preserves original CUDA/uring arguments.
// The sync-return to PLE H2D-entry interval covers reader->Gather for the
// audited shapes, except the first upload where no prior PLE sync exists.
#include <cuda_runtime_api.h>
#include <dlfcn.h>
#include <liburing.h>
#include <sys/syscall.h>
#include <unistd.h>

#include <cerrno>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <ctime>

namespace {
uint64_t Clock(clockid_t id) {
  timespec t{};
  if (clock_gettime(id, &t) != 0) std::abort();
  return uint64_t(t.tv_sec) * 1000000000 + t.tv_nsec;
}
template <class T>
T Next(const char* name) {
  T fn = reinterpret_cast<T>(dlsym(RTLD_NEXT, name));
  if (!fn) std::abort();
  return fn;
}
struct Interval {
  bool active = false;
  uint64_t wall = 0;
  uint64_t cpu = 0;
  uint64_t io_wall = 0;
  uint64_t io_cpu = 0;
  uint64_t calls = 0;
  uint64_t waits = 0;
  uint64_t errors = 0;
  uint64_t sequence = 0;
};
thread_local Interval state;
}  // namespace

extern "C" cudaError_t cudaEventSynchronize(cudaEvent_t event) {
  using Fn = cudaError_t (*)(cudaEvent_t);
  static Fn real = Next<Fn>("cudaEventSynchronize");
  const cudaError_t rc = real(event);
  const int saved_errno = errno;
  state.active = rc == cudaSuccess;
  state.io_wall = state.io_cpu = state.calls = state.waits = state.errors = 0;
  state.wall = Clock(CLOCK_MONOTONIC);
  state.cpu = Clock(CLOCK_THREAD_CPUTIME_ID);
  errno = saved_errno;
  return rc;
}

extern "C" int io_uring_submit_and_wait(io_uring* ring, unsigned wait_nr) {
  using Fn = int (*)(io_uring*, unsigned);
  static Fn real = Next<Fn>("io_uring_submit_and_wait");
  if (!state.active) return real(ring, wait_nr);
  const uint64_t wall = Clock(CLOCK_MONOTONIC);
  const uint64_t cpu = Clock(CLOCK_THREAD_CPUTIME_ID);
  const int rc = real(ring, wait_nr);
  const int saved_errno = errno;
  state.io_cpu += Clock(CLOCK_THREAD_CPUTIME_ID) - cpu;
  state.io_wall += Clock(CLOCK_MONOTONIC) - wall;
  ++state.calls;
  state.waits += wait_nr;
  if (rc < 0) ++state.errors;
  errno = saved_errno;
  return rc;
}

extern "C" cudaError_t cudaMemcpyAsync(void* dst, const void* src, size_t bytes,
                                       cudaMemcpyKind kind,
                                       cudaStream_t stream) {
  using Fn =
      cudaError_t (*)(void*, const void*, size_t, cudaMemcpyKind, cudaStream_t);
  static Fn real = Next<Fn>("cudaMemcpyAsync");
  const bool target = kind == cudaMemcpyHostToDevice &&
                      (bytes == 8192U * 2560 || bytes == 8181U * 2560);
  if (!target) return real(dst, src, bytes, kind, stream);
  const uint64_t wall = Clock(CLOCK_MONOTONIC);
  const uint64_t cpu = Clock(CLOCK_THREAD_CPUTIME_ID);
  const Interval observed = state;
  state.active = false;
  const cudaError_t rc = real(dst, src, bytes, kind, stream);
  const int saved_errno = errno;
  dprintf(
      STDERR_FILENO,
      "[gather-interval] tid=%ld seq=%llu bytes=%zu prior_sync=%d "
      "wall_ns=%llu cpu_ns=%llu io_wall_ns=%llu io_cpu_ns=%llu "
      "io_calls=%llu requested_completions=%llu io_errors=%llu "
      "copy_rc=%d end_wall_ns=%llu\n",
      syscall(SYS_gettid), static_cast<unsigned long long>(state.sequence++),
      bytes, int(observed.active),
      static_cast<unsigned long long>(observed.active ? wall - observed.wall
                                                      : 0),
      static_cast<unsigned long long>(observed.active ? cpu - observed.cpu : 0),
      static_cast<unsigned long long>(observed.io_wall),
      static_cast<unsigned long long>(observed.io_cpu),
      static_cast<unsigned long long>(observed.calls),
      static_cast<unsigned long long>(observed.waits),
      static_cast<unsigned long long>(observed.errors), int(rc),
      static_cast<unsigned long long>(wall));
  errno = saved_errno;
  return rc;
}
