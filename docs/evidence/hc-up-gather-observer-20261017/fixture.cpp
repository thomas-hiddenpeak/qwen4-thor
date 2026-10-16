#include <cuda_runtime_api.h>
#include <liburing.h>
#include <cassert>
#include <cerrno>
#include <cstdio>
#include <ctime>
#include <thread>
#include <cstdint>
uint64_t Cpu() {
  timespec t{}; clock_gettime(CLOCK_THREAD_CPUTIME_ID, &t);
  return uint64_t(t.tv_sec) * 1000000000 + t.tv_nsec;
}
void Copy(size_t bytes) {
  assert(cudaMemcpyAsync(reinterpret_cast<void*>(1),
      reinterpret_cast<void*>(2), bytes, cudaMemcpyHostToDevice, nullptr)
      == cudaSuccess);
  assert(errno == ENOSPC);
}
void Work() {
  Copy(8192U * 2560); // No sync must be marked unavailable.
  assert(cudaEventSynchronize(nullptr) == cudaSuccess && errno == EDOM);
  const uint64_t start = Cpu();
  while (Cpu() - start < 20000000) {}
  assert(io_uring_submit_and_wait(nullptr, 7) == 7 && errno == ERANGE);
  Copy(8192U * 2560);
  assert(cudaEventSynchronize(nullptr) == cudaSuccess);
  assert(io_uring_submit_and_wait(reinterpret_cast<io_uring*>(1), 2) == -EIO);
  assert(errno == EIO);
  Copy(8181U * 2560);
  assert(cudaEventSynchronize(reinterpret_cast<cudaEvent_t>(1)) ==
         cudaErrorInvalidValue && errno == EDOM);
  Copy(8192U * 2560);
  Copy(4); // Unrelated shape must not log.
}
int main() { Work(); std::thread t(Work); t.join(); std::puts("fixture passed"); }
