#include "q4t/runtime/cuda_allocation_quota.h"

#include <cuda_runtime_api.h>

#include <array>
#include <atomic>
#include <cstdio>
#include <limits>
#include <mutex>
#include <new>

#include "cuda_quota_backend.h"

extern "C" cudaError_t __real_cudaMalloc(void**, size_t);
extern "C" cudaError_t __real_cudaMallocManaged(void**, size_t, unsigned int);
extern "C" cudaError_t __real_cudaHostAlloc(void**, size_t, unsigned int);
extern "C" cudaError_t __real_cudaMallocHost(void**, size_t);
extern "C" cudaError_t __real_cudaMallocAsync(void**, size_t, cudaStream_t);
extern "C" cudaError_t __real_cudaFree(void*);
extern "C" cudaError_t __real_cudaFreeHost(void*);
extern "C" cudaError_t __real_cudaFreeAsync(void*, cudaStream_t);
extern "C" cudaError_t __real_cudaStreamCreate(cudaStream_t*);
extern "C" cudaError_t __real_cudaStreamCreateWithFlags(cudaStream_t*,
                                                        unsigned);
extern "C" cudaError_t __real_cudaStreamCreateWithPriority(cudaStream_t*,
                                                           unsigned, int);
extern "C" cudaError_t __real_cudaStreamDestroy(cudaStream_t);
extern "C" cudaError_t __real_cudaStreamSynchronize(cudaStream_t);
extern "C" cudaError_t __real_cudaDeviceSynchronize();

namespace q4t::runtime {
namespace {
using Kind = quota::Kind;
using Release = quota::Release;
using Error = quota::Error;

struct Stream {
  cudaStream_t handle = nullptr;
  uint64_t generation = 0;
};

struct State {
  explicit State(uint64_t limit) : ledger(limit) {}
  quota::Ledger<> ledger;
  std::mutex streams_mutex;
  std::array<Stream, 256> streams{};
  uint64_t next_stream = 2;  // 1 = legacy default stream, 0 = all devices.
  std::atomic<uint64_t> byte_rejections{0};
  std::atomic<uint64_t> entry_rejections{0};
  std::atomic<uint64_t> stream_rejections{0};
  std::atomic<uint64_t> contract_failures{0};
  std::atomic<uint64_t> backend_failures{0};
  std::atomic<size_t> stream_entries{0};
  std::atomic<bool> stream_poisoned{false};
};

// Intentionally process-lived. CUDA process caches may be released by static
// destructors after the server is gone. No reset or metadata destruction may
// discard their charges. The OS owns the fixed metadata allocation at exit.
constinit std::atomic<State*> state{nullptr};
constinit std::atomic<bool> configured{false};
thread_local CudaQuotaRejection last_rejection = CudaQuotaRejection::kNone;
thread_local uint64_t per_thread_stream = 0;

cudaError_t Translate(State* value, quota::Result result) noexcept {
  switch (result.error) {
    case Error::kNone:
      return cudaSuccess;
    case Error::kByteLimit:
      ++value->byte_rejections;
      last_rejection = CudaQuotaRejection::kBytes;
      return cudaErrorMemoryAllocation;
    case Error::kEntryLimit:
      ++value->entry_rejections;
      last_rejection = CudaQuotaRejection::kEntries;
      return cudaErrorMemoryAllocation;
    case Error::kBackendFailure:
      ++value->backend_failures;
      return static_cast<cudaError_t>(result.backend_error);
    default:
      ++value->contract_failures;
      last_rejection = CudaQuotaRejection::kContract;
      // An OOM with ambiguous ownership is not a recoverable capacity refusal.
      return result.backend_error &&
                     result.backend_error != cudaErrorMemoryAllocation
                 ? static_cast<cudaError_t>(result.backend_error)
                 : cudaErrorInvalidValue;
  }
}

uint64_t Queue(State* value, cudaStream_t stream) noexcept {
  if (stream == nullptr || stream == cudaStreamLegacy) return 1;
  const std::lock_guard lock(value->streams_mutex);
  if (stream == cudaStreamPerThread) {
    if (!per_thread_stream &&
        value->next_stream != std::numeric_limits<uint64_t>::max())
      per_thread_stream = value->next_stream++;
    return per_thread_stream;
  }
  for (const auto& entry : value->streams) {
    if (entry.handle == stream) return entry.generation;
  }
  return 0;
}

cudaError_t StreamRejection(State* value) noexcept {
  ++value->stream_rejections;
  last_rejection = CudaQuotaRejection::kStreams;
  return cudaErrorMemoryAllocation;
}

template <typename Call>
cudaError_t Allocate(Kind kind, void** pointer, size_t bytes, Call call) {
  last_rejection = CudaQuotaRejection::kNone;
  State* value = state.load(std::memory_order_acquire);
  if (!value) return call();
  if (!pointer) return cudaErrorInvalidValue;
  *pointer = nullptr;
  if (value->stream_poisoned.load())
    return Translate(value, {Error::kOwnershipUnknown});
  if (bytes == 0) {
    const auto error = call();
    // CUDA's zero-byte contract must not create an uncharged owner.
    if (*pointer) {
      value->stream_poisoned = true;
      *pointer = nullptr;
      return Translate(value, {Error::kOwnershipUnknown});
    }
    return error;
  }
  const auto admitted = value->ledger.Reserve(bytes, kind);
  if (!admitted.ok()) return Translate(value, admitted.result);
  const auto error = call();
  const auto committed =
      value->ledger.CommitAllocation(admitted.ticket, *pointer, error);
  if (!committed.ok()) *pointer = nullptr;
  return Translate(value, committed);
}

template <typename Call>
cudaError_t Free(void* pointer, Release release, uint64_t queue, Call call) {
  State* value = state.load(std::memory_order_acquire);
  if (!value || !pointer) return call();
  const auto admitted = value->ledger.BeginRelease(pointer, release, queue);
  if (!admitted.ok()) return Translate(value, admitted.result);
  const auto error = call();
  return Translate(value, value->ledger.CommitRelease(admitted.ticket, error));
}
}  // namespace

Status ConfigureCudaAllocationQuota(uint64_t limit_bytes) {
  if (configured.exchange(true))
    return Status::Fail("CUDA allocation quota is configured once per process");
  if (limit_bytes) {
    State* created = new (std::nothrow) State(limit_bytes);
    if (!created)
      return Status::Fail("CUDA allocation quota metadata allocation");
    state.store(created, std::memory_order_release);
  }
  ReportCudaAllocationQuota("configured");
  return {};
}

CudaQuotaSnapshot ReadCudaAllocationQuota() noexcept {
  CudaQuotaSnapshot result;
  result.configured = configured.load();
  if (State* value = state.load(std::memory_order_acquire)) {
    result.enabled = true;
    result.memory = value->ledger.Snapshot();
    result.memory.poisoned |= value->stream_poisoned.load();
    result.byte_rejections = value->byte_rejections.load();
    result.entry_rejections = value->entry_rejections.load();
    result.stream_rejections = value->stream_rejections.load();
    result.contract_failures = value->contract_failures.load();
    result.backend_failures = value->backend_failures.load();
    result.stream_entries = value->stream_entries.load();
  }
  return result;
}

CudaQuotaRejection LastCudaQuotaRejection() noexcept { return last_rejection; }
void ClearCudaQuotaRejection() noexcept {
  last_rejection = CudaQuotaRejection::kNone;
}

void ReportCudaAllocationQuota(const char* event) noexcept {
  const auto value = ReadCudaAllocationQuota();
  if (!value.configured) return;
  const auto& m = value.memory;
  std::fprintf(
      stderr,
      "[q4t][cuda-quota] {\"event\":\"%s\",\"enabled\":%s,"
      "\"limit_bytes\":%llu,\"charged_bytes\":%llu,\"live_bytes\":%llu,"
      "\"pending_bytes\":%llu,\"unknown_bytes\":%llu,\"peak_bytes\":%llu,"
      "\"entries\":%zu,\"poisoned\":%s,\"byte_rejections\":%llu,"
      "\"entry_rejections\":%llu,\"stream_rejections\":%llu,"
      "\"contract_failures\":%llu,\"backend_failures\":%llu,"
      "\"scope\":\"process_requested_cuda_bytes\",\"physical_ram_limit\":false}"
      "\n",
      event, value.enabled ? "true" : "false",
      static_cast<unsigned long long>(m.limit_bytes),
      static_cast<unsigned long long>(m.charged_bytes),
      static_cast<unsigned long long>(m.live_bytes),
      static_cast<unsigned long long>(m.pending_bytes),
      static_cast<unsigned long long>(m.unknown_bytes),
      static_cast<unsigned long long>(m.peak_bytes), m.entries,
      m.poisoned ? "true" : "false",
      static_cast<unsigned long long>(value.byte_rejections),
      static_cast<unsigned long long>(value.entry_rejections),
      static_cast<unsigned long long>(value.stream_rejections),
      static_cast<unsigned long long>(value.contract_failures),
      static_cast<unsigned long long>(value.backend_failures));
}

cudaError_t QuotaCudaMalloc(void** pointer, size_t bytes) {
  return Allocate(Kind::kDevice, pointer, bytes,
                  [=] { return __real_cudaMalloc(pointer, bytes); });
}
cudaError_t QuotaCudaMallocManaged(void** pointer, size_t bytes,
                                   unsigned flags) {
  return Allocate(Kind::kManaged, pointer, bytes, [=] {
    return __real_cudaMallocManaged(pointer, bytes, flags);
  });
}
cudaError_t QuotaCudaHostAlloc(void** pointer, size_t bytes, unsigned flags) {
  return Allocate(Kind::kHostAlloc, pointer, bytes,
                  [=] { return __real_cudaHostAlloc(pointer, bytes, flags); });
}
cudaError_t QuotaCudaMallocHost(void** pointer, size_t bytes) {
  return Allocate(Kind::kMallocHost, pointer, bytes,
                  [=] { return __real_cudaMallocHost(pointer, bytes); });
}
cudaError_t QuotaCudaMallocAsync(void** pointer, size_t bytes,
                                 cudaStream_t stream) {
  if (State* value = state.load(std::memory_order_acquire)) {
    if (bytes && !Queue(value, stream)) {
      if (pointer) *pointer = nullptr;
      return StreamRejection(value);
    }
  }
  return Allocate(Kind::kAsyncDevice, pointer, bytes, [=] {
    return __real_cudaMallocAsync(pointer, bytes, stream);
  });
}

cudaError_t QuotaCudaFree(void* pointer) {
  return Free(pointer, Release::kDevice, 0,
              [=] { return __real_cudaFree(pointer); });
}
cudaError_t QuotaCudaFreeHost(void* pointer) {
  return Free(pointer, Release::kHost, 0,
              [=] { return __real_cudaFreeHost(pointer); });
}
cudaError_t QuotaCudaFreeAsync(void* pointer, cudaStream_t stream) {
  State* value = state.load(std::memory_order_acquire);
  const uint64_t queue = value ? Queue(value, stream) : 0;
  return Free(pointer, Release::kAsync, queue,
              [=] { return __real_cudaFreeAsync(pointer, stream); });
}

cudaError_t QuotaCudaStreamCreate(cudaStream_t* stream, unsigned flags,
                                  int priority, int variant) {
  const auto call = [&] {
    if (variant == 0) return __real_cudaStreamCreate(stream);
    if (variant == 1) return __real_cudaStreamCreateWithFlags(stream, flags);
    return __real_cudaStreamCreateWithPriority(stream, flags, priority);
  };
  State* value = state.load(std::memory_order_acquire);
  if (!value) return call();
  if (!stream) return cudaErrorInvalidValue;
  *stream = nullptr;
  const std::lock_guard lock(value->streams_mutex);
  if (value->stream_poisoned.load())
    return Translate(value, {Error::kOwnershipUnknown});
  Stream* available = nullptr;
  for (auto& entry : value->streams) {
    if (!entry.handle) {
      available = &entry;
      break;
    }
  }
  if (!available || value->next_stream == std::numeric_limits<uint64_t>::max())
    return StreamRejection(value);
  const auto error = call();
  if (error != cudaSuccess && !*stream) return error;
  bool duplicate = false;
  for (const auto& entry : value->streams)
    duplicate |= *stream && entry.handle == *stream;
  if (error != cudaSuccess || !*stream || duplicate) {
    value->stream_poisoned = true;
    return Translate(value, {Error::kOwnershipUnknown, error});
  }
  *available = {*stream, value->next_stream++};
  ++value->stream_entries;
  return cudaSuccess;
}

cudaError_t QuotaCudaStreamDestroy(cudaStream_t stream) {
  State* value = state.load(std::memory_order_acquire);
  if (!value) return __real_cudaStreamDestroy(stream);
  const std::lock_guard lock(value->streams_mutex);
  const auto error = __real_cudaStreamDestroy(stream);
  if (error != cudaSuccess) {
    value->stream_poisoned = true;
    return Translate(value, {Error::kOwnershipUnknown, error});
  }
  for (auto& entry : value->streams) {
    if (entry.handle == stream && entry.generation) {
      entry = {};
      --value->stream_entries;
      break;
    }
  }
  // Destroy is not a completion barrier. Old-generation pending stays charged.
  return cudaSuccess;
}

cudaError_t QuotaCudaStreamSynchronize(cudaStream_t stream) {
  State* value = state.load(std::memory_order_acquire);
  if (!value) return __real_cudaStreamSynchronize(stream);
  const uint64_t queue = Queue(value, stream);
  // An untracked stream has no charged async operations. Never interpret its
  // token lookup failure as the device-wide (zero) completion scope.
  if (!queue) return __real_cudaStreamSynchronize(stream);
  const auto fence = value->ledger.Capture(queue);
  const auto error = __real_cudaStreamSynchronize(stream);
  // A concurrent destroy/recreate may recycle the handle before the real
  // synchronize call starts. Its success cannot retire the old generation.
  // Keep that charge until a later device barrier. No mutex spans a GPU wait.
  if (error == cudaSuccess && Queue(value, stream) != queue) return error;
  return Translate(value, value->ledger.Complete(fence, error));
}

cudaError_t QuotaCudaDeviceSynchronize() {
  State* value = state.load(std::memory_order_acquire);
  if (!value) return __real_cudaDeviceSynchronize();
  const auto fence = value->ledger.Capture();
  const auto error = __real_cudaDeviceSynchronize();
  return Translate(value, value->ledger.Complete(fence, error));
}
}  // namespace q4t::runtime
