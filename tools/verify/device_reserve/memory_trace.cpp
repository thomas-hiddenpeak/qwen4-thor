// Independent linkage only. The production allocator and binary are unchanged.
#include <cuda_runtime_api.h>
#include <dlfcn.h>
#include <sys/syscall.h>
#include <unistd.h>

#include <atomic>
#include <cstdarg>
#include <cstdio>
#include <cstdlib>
#include <ctime>
#include <mutex>

#include "owner.h"
#include "reserve_plan.h"

extern "C" cudaError_t __real_cudaMalloc(void**, size_t);
extern "C" cudaError_t __real_cudaFree(void*);
extern "C" cudaError_t __real_cudaHostAlloc(void**, size_t, unsigned int);
extern "C" cudaError_t __real_cudaMallocHost(void**, size_t);
extern "C" cudaError_t __real_cudaFreeHost(void*);
extern "C" cudaError_t __real_cudaMallocAsync(void**, size_t, cudaStream_t);
extern "C" cudaError_t __real_cudaFreeAsync(void*, cudaStream_t);
extern "C" cudaError_t __real_cudaStreamSynchronize(cudaStream_t);
extern "C" cudaError_t __real_cudaDeviceSynchronize();
extern "C" cudaError_t __real_cudaEventRecord(cudaEvent_t, cudaStream_t);
extern "C" cudaError_t __real_cudaEventSynchronize(cudaEvent_t);

namespace {
namespace reserve = q4t::verify::device_reserve;
std::atomic<unsigned long> calls{0};
std::mutex log_mutex;
unsigned long sequence = 0;
unsigned long reserve_sequence = 0;

unsigned long long Pointer(const void* value) {
  return reinterpret_cast<unsigned long long>(value);
}

void WriteLine(const char* line, int count, size_t capacity) {
  if (count <= 0 || static_cast<size_t>(count) >= capacity ||
      write(2, line, count) != count) {
    _exit(125);
  }
}

void ReserveRecord(const char* event, const char* format, ...) {
  char details[1536];
  va_list args;
  va_start(args, format);
  const int size = std::vsnprintf(details, sizeof(details), format, args);
  va_end(args);
  if (size < 0 || size >= static_cast<int>(sizeof(details))) _exit(125);
  std::lock_guard lock(log_mutex);
  timespec now{};
  clock_gettime(CLOCK_MONOTONIC, &now);
  char line[2048];
  const int count = std::snprintf(
      line, sizeof(line),
      "[reserve-trace] {\"seq\":%lu,\"tid\":%ld,\"mono\":%lld.%09ld,"
      "\"event\":\"%s\"%s%s}\n",
      reserve_sequence++, syscall(SYS_gettid),
      static_cast<long long>(now.tv_sec), now.tv_nsec, event,
      size ? "," : "", details);
  WriteLine(line, count, sizeof(line));
}

void Record(const char* api, const char* phase, unsigned long call,
            size_t bytes,
            const void* pointer, const void* stream, unsigned int flags,
            const void* caller, int result, const char* backend = "runtime") {
  std::lock_guard lock(log_mutex);
  timespec now{};
  clock_gettime(CLOCK_MONOTONIC, &now);
  Dl_info info{};
  dladdr(caller, &info);
  char line[768];
  const int count = std::snprintf(
      line, sizeof(line),
      "[memory-trace] {\"seq\":%lu,\"call\":%lu,\"tid\":%ld,"
      "\"api\":\"%s\",\"phase\":\"%s\",\"bytes\":%zu,\"ptr\":%llu,"
      "\"stream\":%llu,\"flags\":%u,\"caller\":%llu,\"rc\":%d,"
      "\"mono\":%lld.%09ld,\"backend\":\"%s\"}\n",
      sequence++, call, syscall(SYS_gettid), api, phase, bytes,
      Pointer(pointer), Pointer(stream), flags,
      Pointer(caller) - Pointer(info.dli_fbase), result,
      static_cast<long long>(now.tv_sec), now.tv_nsec, backend);
  WriteLine(line, count, sizeof(line));
}

struct Backend {
  int Allocate(size_t index, size_t bytes, void** pointer) {
    ReserveRecord("alloc", "\"api\":\"cudaMalloc\",\"phase\":\"begin\","
                  "\"index\":%zu,\"bytes\":%zu,\"ptr\":0,\"rc\":-1",
                  index, bytes);
    const cudaError_t rc = __real_cudaMalloc(pointer, bytes);
    ReserveRecord("alloc", "\"api\":\"cudaMalloc\",\"phase\":\"end\","
                  "\"index\":%zu,\"bytes\":%zu,\"ptr\":%llu,\"rc\":%d",
                  index, bytes, rc == cudaSuccess ? Pointer(*pointer) : 0,
                  static_cast<int>(rc));
    return static_cast<int>(rc);
  }

  int Release(size_t index, size_t bytes, void* pointer,
              reserve::ReleaseReason reason) {
    ReserveRecord("free", "\"api\":\"cudaFree\",\"phase\":\"begin\","
                  "\"index\":%zu,\"bytes\":%zu,\"ptr\":%llu,\"rc\":-1,"
                  "\"reason\":\"%s\"", index, bytes, Pointer(pointer),
                  reserve::ReasonName(reason));
    const cudaError_t rc = __real_cudaFree(pointer);
    ReserveRecord("free", "\"api\":\"cudaFree\",\"phase\":\"end\","
                  "\"index\":%zu,\"bytes\":%zu,\"ptr\":%llu,\"rc\":%d,"
                  "\"reason\":\"%s\"", index, bytes, Pointer(pointer),
                  static_cast<int>(rc), reserve::ReasonName(reason));
    return static_cast<int>(rc);
  }
};

struct Registry {
  std::mutex mutex;
  Backend backend;
  reserve::Owner<Backend> owner{q4t_reserve_plan::kDeviceSizes, backend};
  bool exit_registered = false;
};

Registry& State() {
  // Deliberately process-lived; the explicit exit hook owns final checks.
  static auto* registry = new Registry;
  return *registry;
}

void Report(const char* event, const reserve::Summary& summary) {
  ReserveRecord(event,
      "\"status\":\"%s\",\"count\":%zu,\"allocated\":%zu,"
      "\"consumed\":%zu,\"freed\":%zu,\"unhanded_remaining\":%zu,"
      "\"handed_live\":%zu,\"release_unknown\":%zu,\"error\":\"%s\","
      "\"backend_rc\":%d,\"error_index\":%zu,\"plan_sha256\":\"%s\"",
      summary.result.ok() ? "PASS" : "FAILED", summary.count,
      summary.allocated, summary.consumed, summary.freed,
      summary.unhanded_remaining, summary.handed_live, summary.release_unknown,
      reserve::ErrorName(summary.result.error), summary.result.backend_rc,
      summary.result.index, q4t_reserve_plan::kPlanSha256);
}

[[noreturn]] void Fatal(const char* reason) {
  ReserveRecord("fatal", "\"reason\":\"%s\"", reason);
  _exit(125);
}

void AtExit() {
  try {
    auto& registry = State();
    std::lock_guard lock(registry.mutex);
    const auto result = registry.owner.Finalize();
    Report("summary", result);
    if (!result.result.ok()) _exit(125);
  } catch (...) {
    Fatal("exception_during_finalize");
  }
}

void RegisterExit(Registry& registry) {
  if (!registry.exit_registered) {
    // Call after Prepare: CUDA's initialization registered its cleanup first.
    if (std::atexit(AtExit) != 0) Fatal("atexit_registration_failed");
    registry.exit_registered = true;
  }
}

cudaError_t CudaResult(reserve::Result result) {
  if (result.ok()) return cudaSuccess;
  if (result.backend_rc != 0) return cudaError_t(result.backend_rc);
  return cudaErrorInvalidValue;
}

cudaError_t PinnedGate() {
  auto& registry = State();
  std::lock_guard lock(registry.mutex);
  const auto result = registry.owner.RequirePrepared();
  RegisterExit(registry);
  if (!result.ok()) Report("pinned_gate_failed", registry.owner.Snapshot());
  return CudaResult(result);
}
}  // namespace

extern "C" cudaError_t __wrap_cudaMalloc(void** pointer, size_t bytes) {
  const auto id = calls.fetch_add(1);
  void* caller = __builtin_return_address(0);
  Record("cudaMalloc", "begin", id, bytes, nullptr, nullptr, 0, caller, -1,
         "reservation_dispatch");
  cudaError_t rc = cudaErrorInvalidValue;
  try {
    auto& registry = State();
    std::lock_guard lock(registry.mutex);
    if (!registry.owner.started() && registry.owner.healthy()) {
      ReserveRecord("plan", "\"count\":%zu,\"total_bytes\":%zu,"
                    "\"plan_sha256\":\"%s\",\"source_sha256\":\"%s\"",
                    q4t_reserve_plan::kCount,
                    q4t_reserve_plan::kStartupDeviceBytes,
                    q4t_reserve_plan::kPlanSha256,
                    q4t_reserve_plan::kSourceLogSha256);
      registry.owner.Prepare();
      RegisterExit(registry);
      Report("prepared", registry.owner.Snapshot());
    }
    if (registry.owner.healthy() && registry.owner.prepared()) {
      if (!registry.owner.complete()) {
        const auto result = registry.owner.Take(bytes, pointer);
        rc = CudaResult(result);
        if (result.ok()) {
          ReserveRecord("handoff", "\"index\":%zu,\"bytes\":%zu,\"ptr\":%llu",
                        result.index, bytes, Pointer(*pointer));
        } else {
          Report("handoff_failed", registry.owner.Snapshot());
        }
      } else {
        rc = __real_cudaMalloc(pointer, bytes);
        if (rc == cudaSuccess) {
          rc = CudaResult(registry.owner.ObserveExternalAllocation(*pointer));
        }
      }
    } else {
      const auto result = registry.owner.Snapshot().result;
      rc = CudaResult(result.ok()
                          ? registry.owner.Reject(reserve::Error::kWrongPhase)
                          : result);
    }
  } catch (...) {
    Fatal("exception_during_cudaMalloc");
  }
  Record("cudaMalloc", "end", id, bytes,
         rc == cudaSuccess ? *pointer : nullptr, nullptr, 0, caller, int(rc),
         "reservation_dispatch");
  return rc;
}

extern "C" cudaError_t __wrap_cudaFree(void* pointer) {
  const auto id = calls.fetch_add(1);
  void* caller = __builtin_return_address(0);
  Record("cudaFree", "begin", id, 0, pointer, nullptr, 0, caller, -1);
  cudaError_t rc;
  try {
    auto& registry = State();
    std::lock_guard lock(registry.mutex);
    const auto result = registry.owner.Release(pointer);
    rc = result.error == reserve::Error::kNotOwned
             ? __real_cudaFree(pointer) : CudaResult(result);
  } catch (...) {
    Fatal("exception_during_cudaFree");
  }
  Record("cudaFree", "end", id, 0, pointer, nullptr, 0, caller, int(rc));
  return rc;
}

#define TRACE_PINNED(api, signature, arguments, flags_arg)                     \
  extern "C" cudaError_t __wrap_##api signature {                             \
    const auto id = calls.fetch_add(1);                                        \
    void* caller = __builtin_return_address(0);                                \
    Record(#api, "begin", id, bytes, nullptr, nullptr, flags_arg, caller, -1); \
    cudaError_t rc;                                                           \
    try {                                                                    \
      rc = PinnedGate();                                                      \
      if (rc == cudaSuccess) rc = __real_##api arguments;                      \
    } catch (...) {                                                          \
      Fatal("exception_during_pinned_allocation");                           \
    }                                                                        \
    Record(#api, "end", id, bytes, rc == cudaSuccess ? *p : nullptr,           \
           nullptr, flags_arg, caller, int(rc));                              \
    return rc;                                                               \
  }
TRACE_PINNED(cudaHostAlloc, (void** p, size_t bytes, unsigned int flags),
             (p, bytes, flags), flags)
TRACE_PINNED(cudaMallocHost, (void** p, size_t bytes), (p, bytes), 0)

extern "C" cudaError_t __wrap_cudaMallocAsync(void** pointer, size_t bytes,
                                              cudaStream_t stream) {
  const auto id = calls.fetch_add(1);
  void* caller = __builtin_return_address(0);
  Record("cudaMallocAsync", "begin", id, bytes, nullptr, stream, 0, caller, -1);
  cudaError_t rc = cudaErrorInvalidValue;
  try {
    auto& registry = State();
    std::lock_guard lock(registry.mutex);
    if (!registry.owner.prepared() || !registry.owner.complete() ||
        !registry.owner.healthy()) {
      registry.owner.Reject(reserve::Error::kWrongPhase);
      RegisterExit(registry);
    } else {
      rc = __real_cudaMallocAsync(pointer, bytes, stream);
      if (rc == cudaSuccess) {
        rc = CudaResult(registry.owner.ObserveExternalAllocation(*pointer));
      }
    }
  } catch (...) {
    Fatal("exception_during_cudaMallocAsync");
  }
  Record("cudaMallocAsync", "end", id, bytes,
         rc == cudaSuccess ? *pointer : nullptr, stream, 0, caller, int(rc));
  return rc;
}

extern "C" cudaError_t __wrap_cudaFreeAsync(void* pointer,
                                            cudaStream_t stream) {
  const auto id = calls.fetch_add(1);
  void* caller = __builtin_return_address(0);
  Record("cudaFreeAsync", "begin", id, 0, pointer, stream, 0, caller, -1);
  cudaError_t rc;
  try {
    auto& registry = State();
    std::lock_guard lock(registry.mutex);
    if (registry.owner.Contains(pointer)) {
      // This experiment preserves the original synchronous allocation/free
      // contract; accepting async free would need separate completion tracking.
      rc = CudaResult(registry.owner.Reject(reserve::Error::kWrongPhase));
    } else {
      rc = __real_cudaFreeAsync(pointer, stream);
    }
  } catch (...) {
    Fatal("exception_during_cudaFreeAsync");
  }
  Record("cudaFreeAsync", "end", id, 0, pointer, stream, 0, caller, int(rc));
  return rc;
}

#define TRACE_CALL(api, signature, arguments, ptr_arg, stream_arg)             \
  extern "C" cudaError_t __wrap_##api signature {                             \
    const auto id = calls.fetch_add(1);                                        \
    void* caller = __builtin_return_address(0);                                \
    Record(#api, "begin", id, 0, ptr_arg, stream_arg, 0, caller, -1);          \
    const auto rc = __real_##api arguments;                                   \
    Record(#api, "end", id, 0, ptr_arg, stream_arg, 0, caller, int(rc));       \
    return rc;                                                               \
  }
TRACE_CALL(cudaFreeHost, (void* p), (p), p, nullptr)
TRACE_CALL(cudaStreamSynchronize, (cudaStream_t stream), (stream), nullptr,
           stream)
TRACE_CALL(cudaDeviceSynchronize, (), (), nullptr, nullptr)
TRACE_CALL(cudaEventRecord, (cudaEvent_t event, cudaStream_t stream),
           (event, stream), event, stream)
TRACE_CALL(cudaEventSynchronize, (cudaEvent_t event), (event), event, nullptr)
