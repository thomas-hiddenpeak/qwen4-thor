#include <cuda_runtime_api.h>
#include <liburing.h>
#include <cerrno>
#include <chrono>
#include <thread>
extern "C" cudaError_t cudaEventSynchronize(cudaEvent_t event) {
  std::this_thread::sleep_for(std::chrono::milliseconds(20));
  errno = EDOM;
  return event ? cudaErrorInvalidValue : cudaSuccess;
}
extern "C" int io_uring_submit_and_wait(io_uring* ring, unsigned wait_nr) {
  if (ring) { errno = EIO; return -EIO; }
  std::this_thread::sleep_for(std::chrono::milliseconds(30));
  errno = ERANGE;
  return int(wait_nr);
}
extern "C" cudaError_t cudaMemcpyAsync(void* dst, const void* src, size_t bytes,
                                      cudaMemcpyKind kind, cudaStream_t stream) {
  errno = ENOSPC;
  return dst == reinterpret_cast<void*>(1) && src == reinterpret_cast<void*>(2)
      && bytes > 0 && kind == cudaMemcpyHostToDevice && !stream
      ? cudaSuccess : cudaErrorInvalidValue;
}
