// Production wrappers against deterministic CPU fakes, one case per process.
// Never links libcudart, runs a CUDA context, or accesses a model.
#include "wrapped.h"

#include <array>
#include <atomic>
#include <barrier>
#include <cstdio>
#include <stdexcept>
#include <string>
#include <thread>
#include <utility>
#include <vector>

#include "q4t/runtime/cleanup_policy.h"
#include "q4t/runtime/cuda_allocation_quota.h"
#include "q4t/runtime/startup_plan.h"
#include "q4t/runtime/startup_reservation.h"
#include "../../src/server/cuda_memory_backend.h"

namespace {
namespace rt = q4t::runtime;
using rt::CudaQuotaRejection;
std::atomic<int> allocations{0}, frees{0}, creates{0}, syncs{0};
int allocation_error = 0, release_error = 0, sync_error = 0, create_error = 0;
int pending_error = 0;
bool anomalous_pointer = false;
bool null_success = false;
unsigned observed_flags = 0;
int observed_priority = 0, observed_variant = -1;
uintptr_t stream_value = 4096;
void* fixed_pointer = nullptr;
void (*sync_hook)() = nullptr;

void Check(bool value, const char* message) {
  if (!value) throw std::runtime_error(message);
}
void Ok(cudaError_t error) { Check(error == cudaSuccess, "CUDA result"); }
auto Snapshot() { return rt::ReadCudaAllocationQuota(); }
void Configure(uint64_t limit = 64) {
  Check(rt::ConfigureCudaAllocationQuota(limit).ok(), "configure");
}
void Charged(uint64_t bytes, uint64_t pending = 0) {
  const auto s = Snapshot().memory;
  Check(s.charged_bytes == bytes && s.pending_bytes == pending &&
            !s.unknown_bytes && !s.reserved_bytes && !s.releasing_bytes &&
            !s.poisoned,
        "charge partition");
}
void* Allocate(size_t bytes) {
  void* p = nullptr;
  Ok(__wrap_cudaMalloc(&p, bytes));
  return p;
}
cudaStream_t Stream(int variant = 0) {
  cudaStream_t stream = nullptr;
  if (variant == 0) Ok(__wrap_cudaStreamCreate(&stream));
  if (variant == 1) Ok(__wrap_cudaStreamCreateWithFlags(&stream, 17));
  if (variant == 2) Ok(__wrap_cudaStreamCreateWithPriority(&stream, 9, -2));
  return stream;
}
rt::StartupPlan Plan(std::initializer_list<size_t> sizes) {
  rt::StartupPlan plan;
  for (auto bytes : sizes)
    Check(plan.AddBytes(std::to_string(plan.entries().size()), bytes).ok(),
          "plan");
  return plan;
}

void Passthrough(bool disabled) {
  if (disabled) Configure(0);
  void* p = Allocate(128);
  Ok(__wrap_cudaFree(p));
  Check(!Snapshot().enabled && Snapshot().configured == disabled,
        "disabled state");
  Check(allocations == 1 && frees == 1, "passthrough");
}
void Mixed() {
  Configure();
  std::array<void*, 5> p{};
  Ok(__wrap_cudaMalloc(&p[0], 16));
  Ok(__wrap_cudaMallocManaged(&p[1], 8, 17));
  Check(observed_flags == 17, "managed flags");
  Ok(__wrap_cudaHostAlloc(&p[2], 8, 25));
  Check(observed_flags == 25, "host flags");
  Ok(__wrap_cudaMallocHost(&p[3], 16));
  Ok(__wrap_cudaMallocAsync(&p[4], 16, nullptr));
  Charged(64);
  void* denied = reinterpret_cast<void*>(1);
  Check(__wrap_cudaMalloc(&denied, 1) == cudaErrorMemoryAllocation && !denied &&
            allocations == 5 && Snapshot().byte_rejections == 1 &&
            rt::LastCudaQuotaRejection() == CudaQuotaRejection::kBytes,
        "mixed prebackend denial");
  Ok(__wrap_cudaFree(p[0]));
  Ok(__wrap_cudaFree(p[1]));
  Ok(__wrap_cudaFreeHost(p[2]));
  Ok(__wrap_cudaFreeHost(p[3]));
  Ok(__wrap_cudaFreeAsync(p[4], nullptr));
  Charged(16, 16);
  Ok(__wrap_cudaStreamSynchronize(cudaStreamLegacy));
  Charged(0);
  p[0] = Allocate(64);
  Check(rt::LastCudaQuotaRejection() == CudaQuotaRejection::kNone, "stale TLS");
  Ok(__wrap_cudaFree(p[0]));
}
void Zero() {
  Configure();
  void* p = reinterpret_cast<void*>(1);
  Ok(__wrap_cudaMalloc(&p, 0));
  Check(!p, "zero pointer");
  Ok(__wrap_cudaMallocManaged(&p, 0, 1));
  Ok(__wrap_cudaHostAlloc(&p, 0, 0));
  Ok(__wrap_cudaMallocHost(&p, 0));
  Ok(__wrap_cudaMallocAsync(&p, 0, nullptr));
  Ok(__wrap_cudaFree(nullptr));
  Ok(__wrap_cudaFreeHost(nullptr));
  Ok(__wrap_cudaFreeAsync(nullptr, nullptr));
  Check(__wrap_cudaMalloc(nullptr, 8) == cudaErrorInvalidValue &&
            allocations == 5 && frees == 3,
        "zero/null backend contract");
  Charged(0);
}
void BackendFailure(bool ambiguous) {
  Configure();
  allocation_error = cudaErrorMemoryAllocation;
  anomalous_pointer = ambiguous;
  int* p = nullptr;
  const auto result = q4t::server::detail::AllocateCudaMemory(p, 16, "test");
  Check(!p, "failed ownership transferred");
  Check(result.exhausted() == !ambiguous && !result.quota_exhausted,
        "unknown failure disguised as capacity");
  const auto s = Snapshot();
  Check(s.memory.charged_bytes == (ambiguous ? 16 : 0) &&
            s.memory.poisoned == ambiguous,
        "failed charge");
  if (!ambiguous) {
    allocation_error = 0;
    Ok(__wrap_cudaFree(Allocate(64)));
    Charged(0);
  }
}
void ServeReason() {
  Configure(8);
  int* p = nullptr;
  auto result = q4t::server::detail::AllocateCudaMemory(p, 9, "test");
  Check(result.exhausted() && result.quota_exhausted && allocations == 0 &&
            std::string(result.Reason()) == "cuda_allocation_quota_exhausted",
        "serve quota reason");
  pending_error = cudaErrorIllegalAddress;
  result = q4t::server::detail::AllocateCudaMemory(p, 9, "test");
  Check(!result.exhausted() && !result.quota_exhausted &&
            pending_error == cudaErrorIllegalAddress,
        "hidden device failure");
}
void InvalidOwnership(const std::string& mode) {
  Configure();
  void* p = nullptr;
  if (mode == "null_success") null_success = true;
  if (mode == "zero_owner") anomalous_pointer = true;
  if (mode == "duplicate_owner") {
    fixed_pointer = reinterpret_cast<void*>(64);
    p = Allocate(16);
  }
  Check(__wrap_cudaMalloc(&p, mode == "zero_owner" ? 0 : 16) ==
                cudaErrorInvalidValue &&
            !p && Snapshot().memory.poisoned,
        "unknown allocation accepted");
  const int count = allocations;
  Check(__wrap_cudaMalloc(&p, 1) != cudaSuccess && allocations == count,
        "poisoned allocator reached backend");
}
void StreamFailure(bool duplicate) {
  Configure();
  cudaStream_t stream = nullptr;
  if (duplicate) {
    stream = Stream();
    stream_value = reinterpret_cast<uintptr_t>(stream);
    Check(__wrap_cudaStreamCreate(&stream) == cudaErrorInvalidValue &&
              Snapshot().memory.poisoned,
          "duplicate stream accepted");
    void* p = nullptr;
    Check(__wrap_cudaMalloc(&p, 1) != cudaSuccess && allocations == 0,
          "stream poison ignored");
  } else {
    create_error = cudaErrorMemoryAllocation;
    Check(__wrap_cudaStreamCreate(&stream) == cudaErrorMemoryAllocation &&
              !stream && Snapshot().stream_entries == 0 &&
              !Snapshot().memory.poisoned,
          "known create failure");
    create_error = 0;
    stream = Stream();
    Ok(__wrap_cudaStreamDestroy(stream));
  }
}
void UnknownStream() {
  Configure();
  void* p = nullptr;
  Check(__wrap_cudaMallocAsync(&p, 8, reinterpret_cast<cudaStream_t>(444)) ==
                cudaErrorMemoryAllocation &&
            allocations == 0 && !p && Snapshot().stream_rejections == 1,
        "unknown stream forwarded");
  Charged(0);
}
void Startup(bool reject) {
  Configure(64);
  rt::StartupReservation owner;
  if (reject) {
    Check(!owner.Prepare(Plan({32, 24, 16})).ok(), "startup cap accepted");
    Check(allocations == 2 && frees == 2 && Snapshot().byte_rejections == 1,
          "startup rollback");
    owner.Finalize();
    Charged(0);
    return;
  }
  Check(owner.Prepare(Plan({16, 32})).ok(), "startup prepare");
  Charged(48);
  void* a = Allocate(16);
  void* b = Allocate(32);
  Check(owner.Complete().ok() && allocations == 2, "startup double charge");
  void* extra = Allocate(16);
  Charged(64);
  Ok(__wrap_cudaFree(a));
  Ok(__wrap_cudaFree(b));
  Check(owner.Finalize().ownership.result.ok(), "startup finalize");
  Charged(16);  // A process cache survives the server/owner lifetime.
  Check(!rt::ConfigureCudaAllocationQuota(64).ok(), "fresh second quota");
  void* refused = nullptr;
  Check(__wrap_cudaMalloc(&refused, 49) == cudaErrorMemoryAllocation,
        "process cache charge lost");
  Ok(__wrap_cudaFree(extra));
  Charged(0);
}
void StreamFlags() {
  Configure();
  std::vector<cudaStream_t> streams;
  for (int variant = 0; variant < 3; ++variant) {
    streams.push_back(Stream(variant));
    Check(observed_variant == variant, "stream API variant");
    if (variant) Check(observed_flags == (variant == 1 ? 17u : 9u), "flags");
    if (variant == 2) Check(observed_priority == -2, "priority");
  }
  Check(Snapshot().stream_entries == 3, "stream entries");
  for (auto stream : streams) Ok(__wrap_cudaStreamDestroy(stream));
  Check(Snapshot().stream_entries == 0, "stream removal");
}
void AsyncGenerations(bool reuse_during_sync) {
  Configure();
  auto first = Stream();
  auto other = Stream();
  void* p = nullptr;
  Ok(__wrap_cudaMallocAsync(&p, 64, first));
  Ok(__wrap_cudaFreeAsync(p, first));
  Charged(64, 64);
  Ok(__wrap_cudaEventSynchronize(nullptr));
  Ok(__wrap_cudaEventDestroy(nullptr));
  Ok(__wrap_cudaStreamSynchronize(other));
  Ok(__wrap_cudaStreamSynchronize(nullptr));
  Ok(__wrap_cudaStreamSynchronize(reinterpret_cast<cudaStream_t>(777)));
  Charged(64, 64);
  if (reuse_during_sync) {
    sync_hook = [] {
      auto original = reinterpret_cast<cudaStream_t>(4096);
      Ok(__wrap_cudaStreamDestroy(original));
      stream_value = 4096;
      Check(Stream() == original, "concurrent reused handle");
    };
    Ok(__wrap_cudaStreamSynchronize(first));
  } else {
    Ok(__wrap_cudaStreamDestroy(first));
    Charged(64, 64);
    stream_value = reinterpret_cast<uintptr_t>(first);
    Check(Stream() == first, "reused handle");
    Ok(__wrap_cudaStreamSynchronize(first));
  }
  Charged(64, 64);
  Ok(__wrap_cudaDeviceSynchronize());
  Charged(0);
  Ok(__wrap_cudaStreamDestroy(first));
  Ok(__wrap_cudaStreamDestroy(other));
}
void StreamLimit() {
  Configure();
  std::vector<cudaStream_t> streams;
  for (int i = 0; i < 256; ++i) streams.push_back(Stream());
  cudaStream_t excess = nullptr;
  Check(__wrap_cudaStreamCreate(&excess) == cudaErrorMemoryAllocation &&
            creates == 256 && !excess && Snapshot().stream_rejections == 1,
        "stream prebackend rejection");
  Ok(__wrap_cudaStreamDestroy(streams.back()));
  streams.back() = Stream();
  for (auto stream : streams) Ok(__wrap_cudaStreamDestroy(stream));
}
void EntryLimit() {
  Configure(20000);
  std::vector<void*> pointers;
  for (int i = 0; i < 16384; ++i) pointers.push_back(Allocate(1));
  void* excess = nullptr;
  Check(__wrap_cudaMalloc(&excess, 1) == cudaErrorMemoryAllocation &&
            allocations == 16384 && Snapshot().entry_rejections == 1,
        "entry prebackend rejection");
  for (void* p : pointers) Ok(__wrap_cudaFree(p));
  Charged(0);
}
void Concurrent() {
  Configure(32);
  std::barrier gate(8);
  std::atomic<int> accepted{0}, rejected{0};
  std::vector<std::thread> workers;
  for (int i = 0; i < 8; ++i)
    workers.emplace_back([&] {
      void* p = nullptr;
      const auto rc = __wrap_cudaMalloc(&p, 8);
      if (rc == cudaSuccess)
        ++accepted;
      else if (rc == cudaErrorMemoryAllocation &&
               rt::LastCudaQuotaRejection() == CudaQuotaRejection::kBytes)
        ++rejected;
      gate.arrive_and_wait();
      if (p) Ok(__wrap_cudaFree(p));
    });
  for (auto& worker : workers) worker.join();
  Check(accepted == 4 && rejected == 4 && allocations == 4, "concurrency");
  Charged(0);
}
void PerThread() {
  Configure(64);
  std::barrier gate(2);
  std::atomic<bool> passed{false};
  std::thread owner([&] {
    void* p = nullptr;
    Ok(__wrap_cudaMallocAsync(&p, 64, cudaStreamPerThread));
    Ok(__wrap_cudaFreeAsync(p, cudaStreamPerThread));
    gate.arrive_and_wait();
    gate.arrive_and_wait();
    Ok(__wrap_cudaStreamSynchronize(cudaStreamPerThread));
    passed = Snapshot().memory.charged_bytes == 0;
  });
  gate.arrive_and_wait();
  Ok(__wrap_cudaStreamSynchronize(cudaStreamPerThread));
  Charged(64, 64);
  gate.arrive_and_wait();
  owner.join();
  Check(passed, "per-thread scope");
}
void ReleaseFailure() {
  Configure();
  void* p = Allocate(16);
  release_error = cudaErrorUnknown;
  const rt::HandledDeviceRelease handled;
  Check(__wrap_cudaFree(p) == cudaErrorUnknown, "handled failure");
  Check(__wrap_cudaFree(p) != cudaSuccess && frees == 1, "release retry");
  const auto m = Snapshot().memory;
  Check(m.unknown_bytes == 16 && m.charged_bytes == 16 && m.poisoned,
        "unknown charge lost");
}
void Fatal(const std::string& mode) {
  Configure();
  if (mode == "fatal_async_free") {
    void* p = nullptr;
    Ok(__wrap_cudaMallocAsync(&p, 16, nullptr));
    release_error = cudaErrorUnknown;
    __wrap_cudaFreeAsync(p, nullptr);
  } else if (mode == "fatal_sync") {
    sync_error = cudaErrorIllegalAddress;
    __wrap_cudaDeviceSynchronize();
  } else if (mode == "fatal_destroy") {
    auto stream = Stream();
    release_error = cudaErrorUnknown;
    __wrap_cudaStreamDestroy(stream);
  } else if (mode == "fatal_type") {
    __wrap_cudaFreeHost(Allocate(16));
  } else if (mode == "fatal_planned_async") {
    rt::StartupReservation owner;
    Check(owner.Prepare(Plan({16})).ok(), "prepare");
    void* p = Allocate(16);
    Check(owner.Complete().ok(), "complete");
    __wrap_cudaFreeAsync(p, nullptr);
  }
  throw std::runtime_error("fatal cleanup returned");
}
}  // namespace

extern "C" cudaError_t __real_cudaMalloc(void** p, size_t bytes) {
  const int count = ++allocations;
  if (!p) return cudaErrorInvalidValue;
  *p = (bytes && !allocation_error && !null_success) || anomalous_pointer
           ? (fixed_pointer ? fixed_pointer
                            : reinterpret_cast<void*>(uintptr_t(count) * 64))
           : nullptr;
  return static_cast<cudaError_t>(allocation_error);
}
extern "C" cudaError_t __real_cudaMallocManaged(void** p, size_t n,
                                                unsigned f) {
  observed_flags = f;
  return __real_cudaMalloc(p, n);
}
extern "C" cudaError_t __real_cudaHostAlloc(void** p, size_t n, unsigned f) {
  observed_flags = f;
  return __real_cudaMalloc(p, n);
}
extern "C" cudaError_t __real_cudaMallocHost(void** p, size_t n) {
  return __real_cudaMalloc(p, n);
}
extern "C" cudaError_t __real_cudaMallocAsync(void** p, size_t n,
                                              cudaStream_t) {
  return __real_cudaMalloc(p, n);
}
extern "C" cudaError_t __real_cudaFree(void*) {
  ++frees;
  return static_cast<cudaError_t>(release_error);
}
extern "C" cudaError_t __real_cudaFreeHost(void* p) {
  return __real_cudaFree(p);
}
extern "C" cudaError_t __real_cudaFreeAsync(void* p, cudaStream_t) {
  return __real_cudaFree(p);
}
extern "C" cudaError_t __real_cudaStreamCreate(cudaStream_t* s) {
  ++creates;
  observed_variant = 0;
  *s = create_error ? nullptr : reinterpret_cast<cudaStream_t>(stream_value++);
  return static_cast<cudaError_t>(create_error);
}
extern "C" cudaError_t __real_cudaStreamCreateWithFlags(cudaStream_t* s,
                                                        unsigned flags) {
  const auto result = __real_cudaStreamCreate(s);
  observed_variant = 1;
  observed_flags = flags;
  return result;
}
extern "C" cudaError_t __real_cudaStreamCreateWithPriority(cudaStream_t* s,
                                                           unsigned flags,
                                                           int priority) {
  const auto result = __real_cudaStreamCreateWithFlags(s, flags);
  observed_variant = 2;
  observed_priority = priority;
  return result;
}
extern "C" cudaError_t __real_cudaStreamDestroy(cudaStream_t) {
  return static_cast<cudaError_t>(release_error);
}
extern "C" cudaError_t __real_cudaStreamSynchronize(cudaStream_t) {
  ++syncs;
  if (sync_hook) std::exchange(sync_hook, nullptr)();
  return static_cast<cudaError_t>(sync_error);
}
extern "C" cudaError_t __real_cudaDeviceSynchronize() {
  return __real_cudaStreamSynchronize(nullptr);
}
extern "C" cudaError_t __real_cudaEventSynchronize(cudaEvent_t) {
  return cudaSuccess;
}
extern "C" cudaError_t __real_cudaEventDestroy(cudaEvent_t) {
  return cudaSuccess;
}
// The actual serving adapter calls normal CUDA APIs; route those few symbols
// to the exact production wrapper, without loading libcudart in this fixture.
extern "C" cudaError_t cudaMalloc(void** p, size_t n) {
  return __wrap_cudaMalloc(p, n);
}
extern "C" cudaError_t cudaFree(void* p) { return __wrap_cudaFree(p); }
extern "C" cudaError_t cudaPeekAtLastError() {
  return static_cast<cudaError_t>(pending_error);
}
extern "C" cudaError_t cudaGetLastError() {
  return static_cast<cudaError_t>(std::exchange(pending_error, 0));
}

int main(int argc, char** argv) {
  if (argc != 2) return 2;
  const std::string mode = argv[1];
  try {
    if (mode == "unconfigured")
      Passthrough(false);
    else if (mode == "disabled")
      Passthrough(true);
    else if (mode == "mixed")
      Mixed();
    else if (mode == "zero")
      Zero();
    else if (mode == "backend_failure")
      BackendFailure(false);
    else if (mode == "ambiguous_failure")
      BackendFailure(true);
    else if (mode == "serve_reason")
      ServeReason();
    else if (mode == "null_success" || mode == "zero_owner" ||
             mode == "duplicate_owner")
      InvalidOwnership(mode);
    else if (mode == "stream_backend_failure")
      StreamFailure(false);
    else if (mode == "stream_duplicate")
      StreamFailure(true);
    else if (mode == "unknown_stream")
      UnknownStream();
    else if (mode == "startup")
      Startup(false);
    else if (mode == "startup_reject")
      Startup(true);
    else if (mode == "stream_flags")
      StreamFlags();
    else if (mode == "stream_generations")
      AsyncGenerations(false);
    else if (mode == "stream_sync_race")
      AsyncGenerations(true);
    else if (mode == "stream_limit")
      StreamLimit();
    else if (mode == "entry_limit")
      EntryLimit();
    else if (mode == "concurrent")
      Concurrent();
    else if (mode == "per_thread")
      PerThread();
    else if (mode == "release_failure")
      ReleaseFailure();
    else if (mode.starts_with("fatal_"))
      Fatal(mode);
    else
      return 2;
    rt::ReportCudaAllocationQuota("cpu_end");
    std::printf("PASS %s\n", mode.c_str());
    return 0;
  } catch (const std::exception& error) {
    std::fprintf(stderr, "FAIL %s: %s\n", mode.c_str(), error.what());
    return 1;
  }
}
