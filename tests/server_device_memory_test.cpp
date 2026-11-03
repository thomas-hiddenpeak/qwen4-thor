#include "q4t/server/device_memory.h"

#include <array>

#include "q4t/test.h"

namespace {
using q4t::server::AllocateDeviceMemory;
using q4t::server::ApplyDeviceMemoryFailure;
using q4t::server::DeviceMemoryResult;
using q4t::server::DeviceMemoryState;
using q4t::server::ReleaseDeviceMemory;

struct Backend {
  // Deliberately different from CUDA ABI: policy must compare named constants.
  static constexpr int kSuccess = 0, kExhausted = 17, kInvalidValue = 23;
  int allocated = 0, released = 0, peeked = 0, cleared = 0;
  int allocation_error = 0, release_error = 0, pending = 0, clear_result = 17;
  bool failed_address = false, null_success = false;
  std::array<int, 2> storage{};

  int Allocate(void** pointer, size_t) noexcept {
    ++allocated;
    if ((!allocation_error && !null_success) || failed_address)
      *pointer = storage.data();
    return allocation_error;
  }
  int Release(void*) noexcept {
    ++released;
    return release_error;
  }
  int PeekLastError() noexcept {
    ++peeked;
    return pending;
  }
  int ClearLastError() noexcept {
    ++cleared;
    pending = 0;
    return clear_result;
  }
};
}  // namespace

Q4T_TEST(device_memory_success_preserves_ownership_and_error_state) {
  Backend backend;
  int* pointer = nullptr;
  Q4T_CHECK(AllocateDeviceMemory(backend, pointer, 8).ok());
  Q4T_CHECK(pointer == backend.storage.data() && backend.allocated == 1 &&
            backend.peeked == 0 && backend.cleared == 0);
  Q4T_CHECK(ReleaseDeviceMemory(backend, pointer).ok() && !pointer);
  Q4T_CHECK(ReleaseDeviceMemory(backend, pointer).ok() &&
            backend.released == 1);
  return true;
}

Q4T_TEST(device_memory_oom_consumes_only_known_capacity_error) {
  for (int pending : {Backend::kSuccess, Backend::kExhausted}) {
    Backend backend;
    backend.allocation_error = Backend::kExhausted;
    backend.pending = pending;
    int* pointer = nullptr;
    const auto result = AllocateDeviceMemory(backend, pointer, 8);
    Q4T_CHECK(result.exhausted() && !pointer && backend.peeked == 1 &&
              backend.cleared == (pending == Backend::kExhausted));
    Q4T_CHECK(backend.pending == 0);
  }
  return true;
}

Q4T_TEST(device_memory_oom_cannot_hide_another_pending_failure) {
  Backend backend;
  backend.allocation_error = Backend::kExhausted;
  backend.pending = 719;
  int* pointer = nullptr;
  auto result = AllocateDeviceMemory(backend, pointer, 8);
  Q4T_CHECK(result.state == DeviceMemoryState::kFailure &&
            result.pending_error == 719 && backend.cleared == 0 && !pointer);
  backend.pending = Backend::kExhausted;
  backend.clear_result = 700;
  result = AllocateDeviceMemory(backend, pointer, 8);
  Q4T_CHECK(result.state == DeviceMemoryState::kFailure &&
            result.pending_error == 700 && backend.cleared == 1);
  return true;
}

Q4T_TEST(device_memory_non_capacity_failure_never_falls_back) {
  for (int error : {Backend::kInvalidValue, 700, 719, 999}) {
    Backend backend;
    backend.allocation_error = error;
    int* pointer = nullptr;
    const auto result = AllocateDeviceMemory(backend, pointer, 8);
    Q4T_CHECK(result.state == DeviceMemoryState::kFailure &&
              result.operation_error == error && backend.peeked == 0 &&
              backend.cleared == 0 && !pointer);
  }
  return true;
}

Q4T_TEST(device_memory_unknown_allocation_never_transfers_ownership) {
  Backend backend;
  backend.null_success = true;
  int* pointer = nullptr;
  Q4T_CHECK(AllocateDeviceMemory(backend, pointer, 8).state ==
            DeviceMemoryState::kFailure);
  backend.allocation_error = Backend::kExhausted;
  backend.failed_address = true;
  Q4T_CHECK(AllocateDeviceMemory(backend, pointer, 8).state ==
            DeviceMemoryState::kFailure);
  Q4T_CHECK(!pointer && backend.peeked == 0 && backend.cleared == 0);
  pointer = backend.storage.data();
  const int calls = backend.allocated;
  Q4T_CHECK(AllocateDeviceMemory(backend, pointer, 8).state ==
            DeviceMemoryState::kFailure);
  Q4T_CHECK(pointer == backend.storage.data() && backend.allocated == calls);
  return true;
}

Q4T_TEST(device_memory_failed_release_is_not_retried) {
  Backend backend;
  int* pointer = nullptr;
  Q4T_CHECK(AllocateDeviceMemory(backend, pointer, 8).ok());
  backend.release_error = Backend::kExhausted;
  const auto result = ReleaseDeviceMemory(backend, pointer);
  Q4T_CHECK(result.state == DeviceMemoryState::kFailure && !pointer &&
            result.operation_error == Backend::kExhausted);
  Q4T_CHECK(ReleaseDeviceMemory(backend, pointer).ok() &&
            backend.released == 1);
  Q4T_CHECK(backend.peeked == 0 && backend.cleared == 0);
  return true;
}

Q4T_TEST(device_memory_fatal_service_state_is_monotonic) {
  std::atomic<bool> healthy{true}, failed{false}, stop{false};
  const DeviceMemoryResult exhausted{DeviceMemoryState::kExhausted, 17};
  ApplyDeviceMemoryFailure(exhausted, healthy, failed, stop);
  Q4T_CHECK(healthy && !failed && !stop);
  ApplyDeviceMemoryFailure({DeviceMemoryState::kFailure, 700}, healthy, failed,
                           stop);
  Q4T_CHECK(!healthy && failed && stop);
  ApplyDeviceMemoryFailure({}, healthy, failed, stop);
  ApplyDeviceMemoryFailure(exhausted, healthy, failed, stop);
  Q4T_CHECK(!healthy && failed && stop);
  return true;
}
