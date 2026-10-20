// CPU-only production adapter contracts. All real CUDA symbols are fakes;
// this executable must not link libcudart or invoke a model/planner load.
#include "q4t/runtime/startup_reservation.h"

#include <cuda_runtime_api.h>
#include <fcntl.h>
#include <unistd.h>

#include <array>
#include <cstdio>
#include <cstring>
#include <limits>
#include <stdexcept>
#include <type_traits>

#include "q4t/runtime/startup_plan.h"

extern "C" cudaError_t __wrap_cudaMalloc(void**, size_t);
extern "C" cudaError_t __wrap_cudaFree(void*);
extern "C" cudaError_t __wrap_cudaHostAlloc(void**, size_t, unsigned int);
extern "C" cudaError_t __wrap_cudaMallocHost(void**, size_t);
extern "C" cudaError_t __wrap_cudaMallocAsync(void**, size_t, cudaStream_t);
extern "C" cudaError_t __wrap_cudaFreeAsync(void*, cudaStream_t);

namespace {
namespace reserve = q4t::runtime::reservation;
using q4t::runtime::StartupPlan;
using q4t::runtime::StartupReservation;

void Check(bool condition, const char* message) {
  if (!condition) throw std::runtime_error(message);
}

struct FakeCuda {
  struct Block {
    bool live = false;
  };
  std::array<Block, 32> blocks{};
  std::array<size_t, 64> allocation_bytes{};
  std::array<void*, 64> releases{};
  int allocations = 0;
  int frees = 0;
  int host_allocations = 0;
  int malloc_host_allocations = 0;
  int async_allocations = 0;
  int async_frees = 0;
  int fail_allocation = -1;
  void* fail_free = nullptr;
  unsigned int host_flags = 0;
  cudaStream_t stream = nullptr;
  int host_storage = 0;

  cudaError_t Allocate(void** pointer, size_t bytes) noexcept {
    const int ordinal = allocations++;
    allocation_bytes[ordinal] = bytes;
    if (!pointer) return cudaErrorInvalidValue;
    *pointer = nullptr;
    if (ordinal == fail_allocation) return cudaErrorMemoryAllocation;
    for (auto& block : blocks) {
      if (!block.live) {
        block.live = true;
        *pointer = &block;
        return cudaSuccess;
      }
    }
    return cudaErrorMemoryAllocation;
  }

  cudaError_t Release(void* pointer) noexcept {
    releases[frees++] = pointer;
    if (!pointer) return cudaSuccess;
    if (pointer == fail_free) return cudaErrorUnknown;
    for (auto& block : blocks) {
      if (pointer == &block && block.live) {
        block.live = false;
        return cudaSuccess;
      }
    }
    return cudaErrorInvalidValue;
  }

  int Live() const {
    int count = 0;
    for (const auto& block : blocks) count += block.live;
    return count;
  }
};
FakeCuda fake;

StartupPlan Plan(std::initializer_list<size_t> sizes) {
  StartupPlan plan;
  for (size_t bytes : sizes) {
    Check(plan.AddBytes("block_" + std::to_string(plan.entries().size()), bytes)
              .ok(),
          "construct plan");
  }
  return plan;
}

void* Take(size_t bytes) {
  void* pointer = nullptr;
  Check(__wrap_cudaMalloc(&pointer, bytes) == cudaSuccess, "take failed");
  return pointer;
}

void InactivePassthrough() {
  StartupReservation scope;
  void* pointer = Take(7);
  Check(__wrap_cudaFree(pointer) == cudaSuccess, "plain free");
  Check(__wrap_cudaHostAlloc(&pointer, 3, 17) == cudaSuccess &&
            fake.host_flags == 17,
        "host flags passthrough");
  Check(__wrap_cudaMallocHost(&pointer, 5) == cudaSuccess, "malloc host");
  auto stream = reinterpret_cast<cudaStream_t>(size_t{0x1234});
  Check(__wrap_cudaMallocAsync(&pointer, 8, stream) == cudaSuccess &&
            fake.stream == stream,
        "async allocation passthrough");
  Check(__wrap_cudaFreeAsync(pointer, stream) == cudaSuccess &&
            fake.stream == stream,
        "async free passthrough");
  Check(__wrap_cudaMalloc(nullptr, 1) == cudaErrorInvalidValue,
        "inactive allocator error altered");
  Check(!scope.active() && !scope.prepared(), "inactive scope activated");
}

void StablePhysicalLogicalLifecycle() {
  StartupReservation scope;
  Check(scope.Prepare(Plan({16, 32, 32})).ok(), "prepare");
  Check(fake.allocation_bytes[0] == 32 && fake.allocation_bytes[1] == 32 &&
            fake.allocation_bytes[2] == 16,
        "physical descending");
  const auto a = Take(16);
  const auto b = Take(32);
  const auto c = Take(32);
  Check(a == &fake.blocks[2] && b == &fake.blocks[0] && c == &fake.blocks[1],
        "logical order/equal-size stability");
  Check(scope.Complete().ok(), "complete");
  Check(fake.allocations == 3, "handoff physically allocated again");
  for (void* p : {b, a, c}) {
    Check(__wrap_cudaFree(p) == cudaSuccess, "application release");
  }
  const auto done = scope.Finalize();
  Check(done.ownership.result.ok() && done.ownership.freed == 3 &&
            done.ownership.finalized && done.startup_complete &&
            done.ownership.prepared && !done.active &&
            done.device_bytes == 80 && done.plan_sha256[64] == '\0' &&
            std::strlen(done.plan_sha256.data()) == 64,
        "final summary");
  scope.Finalize();
  Check(fake.frees == 3 && fake.Live() == 0, "finalize double free");
}

void PartialReserveRollback() {
  fake.fail_allocation = 2;
  StartupReservation scope;
  Check(!scope.Prepare(Plan({16, 64, 32})).ok(), "partial prepare accepted");
  Check(fake.frees == 2 && fake.releases[0] == &fake.blocks[1] &&
            fake.releases[1] == &fake.blocks[0],
        "rollback physical reverse");
  void* pointer = reinterpret_cast<void*>(size_t{1});
  Check(__wrap_cudaHostAlloc(&pointer, 8, 0) != cudaSuccess &&
            pointer == nullptr && fake.host_allocations == 0,
        "pinned allocation passed failed prepare");
  Check(__wrap_cudaMallocHost(&pointer, 8) != cudaSuccess &&
            fake.malloc_host_allocations == 0,
        "malloc host failure gate");
  const auto done = scope.Finalize();
  Check(done.ownership.result.error == reserve::Error::kAllocation &&
            done.ownership.result.index == 0 &&
            done.ownership.release_unknown == 0 && fake.Live() == 0 &&
            fake.frees == 2,
        "partial rollback summary/retry");
}

void WrongSizeRejectsWithoutHandoff() {
  StartupReservation scope;
  Check(scope.Prepare(Plan({16, 32})).ok(), "prepare");
  void* pointer = reinterpret_cast<void*>(size_t{1});
  Check(__wrap_cudaMalloc(&pointer, 32) != cudaSuccess && !pointer,
        "wrong logical size accepted");
  const auto done = scope.Finalize();
  Check(done.ownership.result.error == reserve::Error::kSizeMismatch &&
            done.ownership.consumed == 0 && done.ownership.freed == 2,
        "wrong size cleanup");
}

void ExtraAllocationBeforeCompleteRejected() {
  StartupReservation scope;
  Check(scope.Prepare(Plan({16})).ok(), "prepare");
  void* owned = Take(16);
  void* extra = nullptr;
  Check(__wrap_cudaMalloc(&extra, 1) != cudaSuccess && fake.allocations == 1,
        "unplanned allocation escaped before Complete");
  Check(!scope.Complete().ok(), "sticky handoff error cleared");
  Check(__wrap_cudaFree(owned) == cudaSuccess,
        "unhealthy owner blocks cleanup");
  Check(scope.Finalize().ownership.handed_live == 0, "cleanup leak");
}

void AsyncBeforeCompleteRejected() {
  StartupReservation scope;
  Check(scope.Prepare(Plan({16})).ok(), "prepare");
  void* pointer = nullptr;
  Check(__wrap_cudaMallocAsync(&pointer, 1, nullptr) != cudaSuccess &&
            fake.async_allocations == 0,
        "async escaped startup plan");
  Check(scope.Finalize().ownership.freed == 1, "async rejection cleanup");
}

void EarlyCompleteAndPartialLoadCleanup() {
  StartupReservation scope;
  Check(scope.Prepare(Plan({16, 32})).ok(), "prepare");
  void* pointer = Take(16);
  Check(!scope.Complete().ok(), "partial handoff Complete accepted");
  Check(__wrap_cudaFree(pointer) == cudaSuccess, "partial application cleanup");
  Check(scope.Finalize().ownership.freed == 2 && fake.Live() == 0,
        "partial load unhanded rollback");
}

void EarlyReleaseCannotComplete() {
  StartupReservation scope;
  Check(scope.Prepare(Plan({16})).ok(), "prepare");
  Check(__wrap_cudaFree(Take(16)) == cudaSuccess, "early release");
  Check(!scope.Complete().ok(), "early-released plan accepted at ready");
}

void ReuseTombstone(bool asynchronous) {
  StartupReservation scope;
  Check(scope.Prepare(Plan({16})).ok(), "prepare");
  void* pointer = Take(16);
  Check(scope.Complete().ok(), "complete");
  Check(__wrap_cudaFree(pointer) == cudaSuccess, "free reserved pointer");
  void* extra = nullptr;
  const auto allocated = asynchronous
                             ? __wrap_cudaMallocAsync(&extra, 19, nullptr)
                             : __wrap_cudaMalloc(&extra, 19);
  Check(allocated == cudaSuccess && extra == pointer, "fake address reuse");
  const auto freed = asynchronous ? __wrap_cudaFreeAsync(extra, nullptr)
                                  : __wrap_cudaFree(extra);
  Check(freed == cudaSuccess && fake.frees == 2,
        "external reused address mistaken for double free");
  Check(scope.Finalize().ownership.result.ok(), "reuse poisoned ownership");
}
void SyncAddressReuse() { ReuseTombstone(false); }
void AsyncAddressReuse() { ReuseTombstone(true); }

void UnknownReleaseNeverRetried() {
  {
    StartupReservation scope;
    Check(scope.Prepare(Plan({16})).ok(), "prepare");
    void* pointer = Take(16);
    Check(scope.Complete().ok(), "complete");
    fake.fail_free = pointer;
    Check(__wrap_cudaFree(pointer) == cudaErrorUnknown, "release rc lost");
    Check(__wrap_cudaFree(pointer) != cudaSuccess && fake.frees == 1,
          "unknown release retried");
    const auto done = scope.Finalize();
    Check(done.ownership.release_unknown == 1 &&
              done.ownership.result.backend_rc == int(cudaErrorUnknown),
          "unknown release disappeared");
  }
  Check(fake.frees == 1, "destructor retried unknown completion");
}

void RollbackReleaseUnknownNeverRetried() {
  fake.fail_allocation = 1;
  fake.fail_free = &fake.blocks[0];
  {
    StartupReservation scope;
    Check(!scope.Prepare(Plan({16, 32})).ok(), "failed reserve accepted");
    const auto done = scope.Finalize();
    Check(done.ownership.result.error == reserve::Error::kAllocation &&
              done.ownership.release_unknown == 1 && fake.frees == 1,
          "rollback unknown release lost original error or retried");
  }
  Check(fake.frees == 1, "destructor retried rollback unknown completion");
}

void AsyncPlannedFreeRejected() {
  StartupReservation scope;
  Check(scope.Prepare(Plan({16})).ok(), "prepare");
  void* pointer = Take(16);
  Check(scope.Complete().ok(), "complete");
  Check(__wrap_cudaFreeAsync(pointer, nullptr) != cudaSuccess &&
            fake.async_frees == 0,
        "planned pointer freed asynchronously");
  Check(__wrap_cudaFree(pointer) == cudaSuccess, "sync cleanup blocked");
}

void DuplicateFreeRejected() {
  StartupReservation scope;
  Check(scope.Prepare(Plan({16})).ok(), "prepare");
  void* pointer = Take(16);
  Check(scope.Complete().ok(), "complete");
  Check(__wrap_cudaFree(pointer) == cudaSuccess, "first free");
  Check(__wrap_cudaFree(pointer) != cudaSuccess && fake.frees == 1,
        "double free forwarded");
}

void ScopeCollisionDoesNotStealOwner() {
  StartupReservation first;
  Check(first.Prepare(Plan({16})).ok(), "prepare first");
  {
    StartupReservation second;
    Check(!second.Prepare(Plan({32})).ok() && !second.active(),
          "second scope stole active registry");
  }
  void* pointer = Take(16);
  Check(first.Complete().ok(), "first scope poisoned by collision");
  Check(__wrap_cudaFree(pointer) == cudaSuccess, "first scope release");
  Check(first.Finalize().ownership.result.ok(), "first final result");
}

void DestructorOrderingAndReuse() {
  struct Application {
    void* pointer = nullptr;
    ~Application() { __wrap_cudaFree(pointer); }
  };
  struct Server {
    StartupReservation reservation;
    Application application;
  };
  std::array<char, 65> digest{};
  for (int i = 0; i < 2; ++i) {
    Server server;
    Check(server.reservation.Prepare(Plan({16})).ok(), "server prepare");
    server.application.pointer = Take(16);
    Check(server.reservation.Complete().ok(), "server Complete");
    const auto current = server.reservation.Snapshot().plan_sha256;
    if (i == 0) digest = current;
    Check(current == digest, "identical plan digest changed");
  }
  Check(fake.frees == 2 && fake.Live() == 0, "member destruction/restart");
}

void RuntimeAllocationFailurePreservesCleanup() {
  StartupReservation scope;
  Check(scope.Prepare(Plan({16})).ok(), "prepare");
  void* pointer = Take(16);
  Check(scope.Complete().ok(), "complete");
  fake.fail_allocation = 1;
  void* extra = nullptr;
  Check(__wrap_cudaMalloc(&extra, 128) == cudaErrorMemoryAllocation,
        "runtime allocator error changed");
  Check(__wrap_cudaFree(pointer) == cudaSuccess, "runtime OOM blocked cleanup");
  Check(scope.Finalize().ownership.result.ok(), "runtime OOM poisons plan");
}

struct IndexedBackend {
  int calls = 0;
  int Allocate(size_t, size_t, void**) noexcept {
    ++calls;
    return 2;
  }
  int Release(size_t, size_t, void*, reserve::ReleaseReason) noexcept {
    ++calls;
    return 0;
  }
};

void InvalidPlansNeverAllocate() {
  IndexedBackend backend;
  for (const auto& sizes :
       {std::vector<size_t>{}, std::vector<size_t>{0},
        std::vector<size_t>{std::numeric_limits<size_t>::max(), 1}}) {
    reserve::Owner owner(sizes, backend);
    Check(owner.Prepare().error == reserve::Error::kInvalidPlan,
          "invalid plan accepted");
    Check(backend.calls == 0, "invalid metadata touched CUDA backend");
  }
}

void StderrFailureDoesNotAbortCleanup() {
  const int saved = dup(STDERR_FILENO);
  const int full = open("/dev/full", O_WRONLY | O_CLOEXEC);
  Check(saved >= 0 && full >= 0, "redirect descriptors");
  Check(dup2(full, STDERR_FILENO) >= 0, "redirect stderr");
  close(full);
  bool accepted = false;
  {
    StartupReservation scope;
    accepted = scope.Prepare(Plan({16})).ok();
    void* pointer = Take(16);
    accepted = accepted && scope.Complete().ok();
    accepted = accepted && __wrap_cudaFree(pointer) == cudaSuccess;
    accepted = accepted && scope.Finalize().ownership.result.ok();
  }
  const int restored = dup2(saved, STDERR_FILENO);
  close(saved);
  clearerr(stderr);
  Check(restored >= 0 && accepted && fake.Live() == 0,
        "stderr failure altered cleanup/control flow");
}
}  // namespace

extern "C" cudaError_t __real_cudaMalloc(void** pointer, size_t bytes) {
  return fake.Allocate(pointer, bytes);
}
extern "C" cudaError_t __real_cudaFree(void* pointer) {
  return fake.Release(pointer);
}
extern "C" cudaError_t __real_cudaHostAlloc(void** pointer, size_t,
                                            unsigned int flags) {
  ++fake.host_allocations;
  fake.host_flags = flags;
  *pointer = &fake.host_storage;
  return cudaSuccess;
}
extern "C" cudaError_t __real_cudaMallocHost(void** pointer, size_t) {
  ++fake.malloc_host_allocations;
  *pointer = &fake.host_storage;
  return cudaSuccess;
}
extern "C" cudaError_t __real_cudaMallocAsync(void** pointer, size_t bytes,
                                              cudaStream_t stream) {
  ++fake.async_allocations;
  fake.stream = stream;
  return fake.Allocate(pointer, bytes);
}
extern "C" cudaError_t __real_cudaFreeAsync(void* pointer,
                                            cudaStream_t stream) {
  ++fake.async_frees;
  fake.stream = stream;
  return fake.Release(pointer);
}

int main() {
  static_assert(std::is_nothrow_destructible_v<StartupReservation>);
  static_assert(noexcept(std::declval<StartupReservation&>().Finalize()));
  const std::pair<const char*, void (*)()> tests[] = {
      {"inactive_passthrough", InactivePassthrough},
      {"stable_physical_logical_lifecycle", StablePhysicalLogicalLifecycle},
      {"partial_reserve_rollback", PartialReserveRollback},
      {"wrong_size", WrongSizeRejectsWithoutHandoff},
      {"unplanned_before_complete", ExtraAllocationBeforeCompleteRejected},
      {"async_before_complete", AsyncBeforeCompleteRejected},
      {"partial_load_cleanup", EarlyCompleteAndPartialLoadCleanup},
      {"early_release", EarlyReleaseCannotComplete},
      {"sync_address_reuse", SyncAddressReuse},
      {"async_address_reuse", AsyncAddressReuse},
      {"unknown_release", UnknownReleaseNeverRetried},
      {"rollback_unknown_release", RollbackReleaseUnknownNeverRetried},
      {"planned_async_free", AsyncPlannedFreeRejected},
      {"duplicate_free", DuplicateFreeRejected},
      {"scope_collision", ScopeCollisionDoesNotStealOwner},
      {"destructor_order_restart", DestructorOrderingAndReuse},
      {"runtime_allocation_failure", RuntimeAllocationFailurePreservesCleanup},
      {"invalid_plans", InvalidPlansNeverAllocate},
      {"stderr_failure", StderrFailureDoesNotAbortCleanup},
  };
  for (const auto& [name, test] : tests) {
    fake = {};
    try {
      test();
      std::printf("PASS %s\n", name);
    } catch (const std::exception& error) {
      std::fprintf(stderr, "FAIL %s: %s\n", name, error.what());
      return 1;
    }
  }
  std::printf("PASS %zu CPU-only production reservation contracts\n",
              std::size(tests));
  return 0;
}
