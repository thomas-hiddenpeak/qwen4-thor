#pragma once

#include <cuda_runtime_api.h>

#include "q4t/runtime/cleanup_policy.h"
#include "q4t/server/device_memory.h"

#ifdef Q4T_SERVE_FAULT_INJECTION
#include <atomic>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#endif

namespace q4t::server::detail {

// No environment-controlled allocator or test hook exists in production.
inline int DeviceMemoryFault(const char* site, const char* request) noexcept {
#ifdef Q4T_SERVE_FAULT_INJECTION
  static std::atomic<bool> fired{false};
  const char* selected = std::getenv("Q4T_SERVE_DEVICE_FAULT");
  const char* selected_request = std::getenv("Q4T_SERVE_REQUEST");
  if (!selected || std::strcmp(selected, site) ||
      (selected_request &&
       (!request || std::strcmp(selected_request, request))))
    return cudaSuccess;
  const char* kind = std::getenv("Q4T_SERVE_DEVICE_ERROR");
  const int error = kind && !std::strcmp(kind, "oom")
                        ? cudaErrorMemoryAllocation
                        : cudaErrorInvalidValue;
  if (fired.exchange(true)) return cudaSuccess;
  std::fprintf(stderr, "[serve-test-device] site=%s error=%d\n", site, error);
  return error;
#else
  (void)site;
  (void)request;
  return cudaSuccess;
#endif
}

struct CudaMemoryBackend {
  static constexpr int kSuccess = cudaSuccess;
  static constexpr int kExhausted = cudaErrorMemoryAllocation;
  static constexpr int kInvalidValue = cudaErrorInvalidValue;
  const char* site;
  const char* request;

  int Allocate(void** pointer, size_t bytes) noexcept {
    const int fault = DeviceMemoryFault(site, request);
    return fault != kSuccess ? fault
                             : static_cast<int>(cudaMalloc(pointer, bytes));
  }
  int Release(void* pointer) noexcept {
    const runtime::CleanupPhase phase(site);
    const runtime::HandledDeviceRelease handled;
    const int fault = DeviceMemoryFault(site, request);
    return fault != kSuccess ? fault : static_cast<int>(cudaFree(pointer));
  }
  int PeekLastError() noexcept { return cudaPeekAtLastError(); }
  int ClearLastError() noexcept { return cudaGetLastError(); }
};

template <typename T>
DeviceMemoryResult AllocateCudaMemory(T*& pointer, size_t bytes,
                                      const char* site,
                                      const char* request = nullptr) noexcept {
  CudaMemoryBackend backend{site, request};
  return AllocateDeviceMemory(backend, pointer, bytes);
}

template <typename T>
DeviceMemoryResult ReleaseCudaMemory(T*& pointer, const char* site,
                                     const char* request = nullptr) noexcept {
  CudaMemoryBackend backend{site, request};
  return ReleaseDeviceMemory(backend, pointer);
}

}  // namespace q4t::server::detail
