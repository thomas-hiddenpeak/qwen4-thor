// Host-only executable: every __real_* CUDA operation below is a CPU fake.
#include <cuda_runtime_api.h>

#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <unordered_set>

extern "C" cudaError_t __wrap_cudaMalloc(void**, size_t);
extern "C" cudaError_t __wrap_cudaFree(void*);
extern "C" cudaError_t __wrap_cudaHostAlloc(void**, size_t, unsigned int);
extern "C" cudaError_t __wrap_cudaFreeHost(void*);

namespace {
std::unordered_set<void*> live;
size_t allocations = 0;
const char* mode = "success";

void Check(bool value) {
  if (!value) std::abort();
}

void RuntimeExit() {
  // Registered by the first real allocation, before the wrapper exit hook.
  Check(live.empty());
  std::fputs("[fake-runtime] finalized\n", stderr);
}
}  // namespace

extern "C" cudaError_t __real_cudaMalloc(void** pointer, size_t bytes) {
  if (allocations++ == 0) Check(std::atexit(RuntimeExit) == 0);
  if (std::strcmp(mode, "allocation_failure") == 0 && allocations == 2) {
    *pointer = nullptr;
    return cudaErrorMemoryAllocation;
  }
  *pointer = std::malloc(bytes);
  Check(*pointer != nullptr && live.insert(*pointer).second);
  return cudaSuccess;
}

extern "C" cudaError_t __real_cudaFree(void* pointer) {
  if (pointer) {
    Check(live.erase(pointer) == 1);
    std::free(pointer);
  }
  return cudaSuccess;
}

extern "C" cudaError_t __real_cudaHostAlloc(void** pointer, size_t bytes,
                                            unsigned int) {
  Check(allocations >= 3);
  *pointer = std::malloc(bytes);
  Check(*pointer != nullptr && live.insert(*pointer).second);
  return cudaSuccess;
}

extern "C" cudaError_t __real_cudaMallocHost(void** pointer, size_t bytes) {
  return __real_cudaHostAlloc(pointer, bytes, 0);
}

extern "C" cudaError_t __real_cudaFreeHost(void* pointer) {
  return __real_cudaFree(pointer);
}

extern "C" cudaError_t __real_cudaMallocAsync(void** pointer, size_t bytes,
                                              cudaStream_t) {
  return __real_cudaMalloc(pointer, bytes);
}

extern "C" cudaError_t __real_cudaFreeAsync(void* pointer, cudaStream_t) {
  return __real_cudaFree(pointer);
}

extern "C" cudaError_t __real_cudaStreamSynchronize(cudaStream_t) {
  return cudaSuccess;
}
extern "C" cudaError_t __real_cudaDeviceSynchronize() { return cudaSuccess; }
extern "C" cudaError_t __real_cudaEventRecord(cudaEvent_t, cudaStream_t) {
  return cudaSuccess;
}
extern "C" cudaError_t __real_cudaEventSynchronize(cudaEvent_t) {
  return cudaSuccess;
}

int main(int argc, char** argv) {
  Check(argc == 2);
  mode = argv[1];
  void* pointer = nullptr;
  if (std::strcmp(mode, "early_pinned") == 0) {
    Check(__wrap_cudaHostAlloc(&pointer, 8, 0) == cudaErrorInvalidValue);
    return 0;  // The wrapper must turn this into a failed acceptance exit.
  }
  if (std::strcmp(mode, "allocation_failure") == 0) {
    Check(__wrap_cudaMalloc(&pointer, 16) == cudaErrorMemoryAllocation);
    Check(live.empty());  // Rollback precedes returning the CUDA error.
    return 0;
  }
  if (std::strcmp(mode, "mismatch") == 0) {
    Check(__wrap_cudaMalloc(&pointer, 32) == cudaErrorInvalidValue);
    return 0;
  }
  void* device[3]{};
  Check(__wrap_cudaMalloc(&device[0], 16) == cudaSuccess);
  Check(allocations == 3);  // Every physical device allocation already exists.
  if (std::strcmp(mode, "partial") == 0) {
    Check(__wrap_cudaFree(device[0]) == cudaSuccess);
    return 0;
  }
  void* host = nullptr;
  Check(__wrap_cudaHostAlloc(&host, 8, 0) == cudaSuccess);
  Check(__wrap_cudaMalloc(&device[1], 32) == cudaSuccess);
  Check(__wrap_cudaMalloc(&device[2], 48) == cudaSuccess);
  std::fputs("serving on port 1\n", stderr);
  for (void* block : device) Check(__wrap_cudaFree(block) == cudaSuccess);
  Check(__wrap_cudaFreeHost(host) == cudaSuccess);
  // Exercise both post-plan passthrough calls, including begin/end identity.
  for (size_t bytes : {size_t{16}, size_t{32}}) {
    Check(__wrap_cudaMalloc(&pointer, bytes) == cudaSuccess);
    Check(__wrap_cudaFree(pointer) == cudaSuccess);
  }
  Check(allocations == 5 && live.empty());
  return 0;
}
