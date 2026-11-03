// Allocation failures are recoverable only when capacity exhaustion is known.
// CUDA-free policy: ownership is relinquished once; an unknown free is fatal.
#pragma once

#include <atomic>
#include <cstddef>
#include <utility>

namespace q4t::runtime {

enum class DeviceMemoryState { kSuccess, kExhausted, kFailure };

struct DeviceMemoryResult {
  DeviceMemoryState state = DeviceMemoryState::kSuccess;
  int operation_error = 0;
  int pending_error = 0;

  bool ok() const { return state == DeviceMemoryState::kSuccess; }
  bool exhausted() const { return state == DeviceMemoryState::kExhausted; }
  const char* Reason() const {
    return ok()          ? "success"
           : exhausted() ? "device_memory_exhausted"
                         : "device_memory_failure";
  }
};

// Backend constants are named, not assumed to match CUDA's numeric ABI. Pure
// host tests exercise the policy without loading a driver or exhausting RAM.
template <typename Backend, typename T>
DeviceMemoryResult AllocateDeviceMemory(Backend& backend, T*& pointer,
                                        size_t bytes) noexcept {
  if (pointer != nullptr)
    return {DeviceMemoryState::kFailure, Backend::kInvalidValue};
  void* allocated = nullptr;
  const int error = backend.Allocate(&allocated, bytes);
  if (error == Backend::kSuccess) {
    if (bytes && !allocated)
      return {DeviceMemoryState::kFailure, Backend::kInvalidValue};
    pointer = static_cast<T*>(allocated);
    return {};
  }
  // A failed operation does not transfer ownership, even if a faulty backend
  // writes an address. Process teardown must resolve that unknown outcome.
  if (error != Backend::kExhausted || allocated != nullptr)
    return {DeviceMemoryState::kFailure, error};
  int pending = backend.PeekLastError();
  if (pending == Backend::kExhausted) pending = backend.ClearLastError();
  // Consume only the known OOM indicator. Never erase another asynchronous
  // error to make an optional fallback appear safe.
  if (pending != Backend::kSuccess && pending != Backend::kExhausted)
    return {DeviceMemoryState::kFailure, error, pending};
  return {DeviceMemoryState::kExhausted, error, pending};
}

template <typename Backend, typename T>
DeviceMemoryResult ReleaseDeviceMemory(Backend& backend, T*& pointer) noexcept {
  auto* owned = std::exchange(pointer, nullptr);
  if (!owned) return {};
  const int error = backend.Release(owned);
  return error == Backend::kSuccess
             ? DeviceMemoryResult{}
             : DeviceMemoryResult{DeviceMemoryState::kFailure, error};
}

inline void ApplyDeviceMemoryFailure(
    const DeviceMemoryResult& result, std::atomic<bool>& gpu_healthy,
    std::atomic<bool>& serving_failed,
    std::atomic<bool>& stop_requested) noexcept {
  if (result.state != DeviceMemoryState::kFailure) return;
  gpu_healthy.store(false, std::memory_order_relaxed);
  serving_failed.store(true);
  stop_requested.store(true);
}

}  // namespace q4t::runtime
