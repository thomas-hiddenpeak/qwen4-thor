// Four bounded CUDA contracts. Run only under the complete stage GPU lease.
#include "q4t/runtime/allocation_quota.h"

#include <cuda_runtime_api.h>
#include <signal.h>
#include <sys/prctl.h>
#include <unistd.h>

#include <algorithm>
#include <array>
#include <atomic>
#include <barrier>
#include <charconv>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <thread>
#include <vector>

#include "q4t/runtime/reservation_owner.h"

namespace {
namespace quota = q4t::runtime::quota;
using quota::Kind;
using quota::Release;
constexpr size_t kMiB = 1024 * 1024;

void Check(bool condition, const char* what) {
  if (!condition) {
    std::fprintf(stderr, "FAIL %s\n", what);
    std::_Exit(1);
  }
}
void Cuda(cudaError_t error) {
  if (error != cudaSuccess) {
    std::fprintf(stderr, "CUDA error %d: %s\n", static_cast<int>(error),
                 cudaGetErrorString(error));
    std::_Exit(1);
  }
}
void Okay(quota::Result result) {
  if (!result.ok()) {
    std::fprintf(stderr, "quota error %s / backend %d\n",
                 quota::ErrorName(result.error), result.backend_error);
    std::_Exit(1);
  }
}

struct Backend {
  std::array<cudaStream_t, 2> streams{};
  std::atomic<int> allocations{0};
  std::atomic<int> frees{0};
  std::atomic<int> synchronizations{0};

  cudaStream_t Stream(uint64_t queue) const noexcept {
    Check(queue > 0 && queue <= streams.size() && streams[queue - 1],
          "known stream generation");
    return streams[queue - 1];
  }
  int Allocate(Kind kind, size_t bytes, void** pointer,
               uint64_t queue) noexcept {
    ++allocations;
    switch (kind) {
      case Kind::kDevice:
        return cudaMalloc(pointer, bytes);
      case Kind::kHostAlloc:
        return cudaHostAlloc(pointer, bytes, cudaHostAllocDefault);
      case Kind::kMallocHost:
        return cudaMallocHost(pointer, bytes);
      case Kind::kManaged:
        return cudaMallocManaged(pointer, bytes, cudaMemAttachGlobal);
      case Kind::kAsyncDevice:
        return cudaMallocAsync(pointer, bytes, Stream(queue));
    }
    return cudaErrorInvalidValue;
  }
  int Release(quota::Release release, void* pointer, uint64_t queue) noexcept {
    ++frees;
    switch (release) {
      case quota::Release::kDevice:
        return cudaFree(pointer);
      case quota::Release::kHost:
        return cudaFreeHost(pointer);
      case quota::Release::kAsync:
        return cudaFreeAsync(pointer, Stream(queue));
    }
    return cudaErrorInvalidValue;
  }
  int Synchronize(uint64_t queue) noexcept {
    ++synchronizations;
    return queue ? cudaStreamSynchronize(Stream(queue))
                 : cudaDeviceSynchronize();
  }
};

using Ledger = quota::Ledger<64>;
using Allocator = quota::Allocator<Backend, 64>;

void Record(const char* label, const Ledger& ledger, const Backend& backend,
            uint64_t expected_bytes, size_t expected_entries) {
  const auto s = ledger.Snapshot();
  Check(s.charged_bytes == expected_bytes && s.entries == expected_entries &&
            s.charged_bytes <= s.limit_bytes && s.peak_bytes <= s.limit_bytes &&
            s.unknown_bytes == 0 && !s.poisoned &&
            s.charged_bytes == s.reserved_bytes + s.live_bytes +
                                   s.releasing_bytes + s.pending_bytes +
                                   s.unknown_bytes,
        label);
  std::printf(
      "{\"event\":\"quota\",\"label\":\"%s\",\"limit\":%llu,"
      "\"charged\":%llu,\"pending\":%llu,\"peak\":%llu,"
      "\"entries\":%zu,\"allocations\":%d,\"frees\":%d,\"syncs\":%d,"
      "\"unknown\":%llu,\"reserved\":%llu,\"live\":%llu,"
      "\"releasing\":%llu,\"poisoned\":%s}\n",
      label, static_cast<unsigned long long>(s.limit_bytes),
      static_cast<unsigned long long>(s.charged_bytes),
      static_cast<unsigned long long>(s.pending_bytes),
      static_cast<unsigned long long>(s.peak_bytes), s.entries,
      backend.allocations.load(), backend.frees.load(),
      backend.synchronizations.load(),
      static_cast<unsigned long long>(s.unknown_bytes),
      static_cast<unsigned long long>(s.reserved_bytes),
      static_cast<unsigned long long>(s.live_bytes),
      static_cast<unsigned long long>(s.releasing_bytes),
      s.poisoned ? "true" : "false");
  std::fflush(stdout);
}

// Whole-buffer validation, not just a pointer/API return. This host copy is
// explicitly outside the requested-CUDA-byte ledger and is at most 32 MiB.
void Verify(void* pointer, size_t bytes, unsigned char value,
            cudaStream_t stream = nullptr) {
  std::vector<unsigned char> copy(bytes);
  Cuda(cudaMemsetAsync(pointer, value, bytes, stream));
  Cuda(cudaMemcpyAsync(copy.data(), pointer, bytes, cudaMemcpyDefault, stream));
  Cuda(cudaStreamSynchronize(stream));
  Check(
      std::all_of(copy.begin(), copy.end(), [=](auto x) { return x == value; }),
      "whole buffer write/read");
  std::printf("{\"event\":\"verified\",\"bytes\":%zu,\"value\":%u}\n", bytes,
              static_cast<unsigned>(value));
}

void Mixed() {
  Ledger ledger(32 * kMiB);
  Backend backend;
  Allocator api(ledger, backend);
  const std::array kinds{Kind::kDevice, Kind::kHostAlloc, Kind::kMallocHost,
                         Kind::kManaged};
  std::array<void*, 4> pointers{};
  for (size_t i = 0; i < kinds.size(); ++i) {
    Okay(api.Allocate(kinds[i], 8 * kMiB, &pointers[i]));
    Verify(pointers[i], 8 * kMiB, static_cast<unsigned char>(i + 31));
  }
  Record("mixed_full", ledger, backend, 32 * kMiB, 4);
  void* denied = nullptr;
  Check(api.Allocate(Kind::kDevice, kMiB, &denied).error ==
                quota::Error::kByteLimit &&
            !denied && backend.allocations == 4,
        "mixed rejection before CUDA");
  Record("mixed_denied", ledger, backend, 32 * kMiB, 4);
  for (size_t i = 0; i < pointers.size(); ++i)
    Okay(api.Free(pointers[i],
                  i == 1 || i == 2 ? Release::kHost : Release::kDevice));
  Record("mixed_released", ledger, backend, 0, 0);
  void* recovery = nullptr;
  Okay(api.Allocate(Kind::kDevice, 32 * kMiB, &recovery));
  Verify(recovery, 32 * kMiB, 97);
  Okay(api.Free(recovery, Release::kDevice));
  Record("mixed_recovered", ledger, backend, 0, 0);
}

void Async() {
  Ledger ledger(16 * kMiB);
  Backend backend;
  for (auto& stream : backend.streams)
    Cuda(cudaStreamCreateWithFlags(&stream, cudaStreamNonBlocking));
  Allocator api(ledger, backend);
  std::array<void*, 2> pointers{};
  for (size_t i = 0; i < pointers.size(); ++i) {
    Okay(api.Allocate(Kind::kAsyncDevice, 8 * kMiB, &pointers[i], i + 1));
    Verify(pointers[i], 8 * kMiB, static_cast<unsigned char>(i + 61),
           backend.streams[i]);
  }
  Okay(api.Free(pointers[0], Release::kAsync, 1));
  Record("async_pending", ledger, backend, 16 * kMiB, 2);
  void* denied = nullptr;
  Check(api.Allocate(Kind::kDevice, 1, &denied).error ==
                quota::Error::kByteLimit &&
            !denied && backend.allocations == 2,
        "async pending not credited");
  Okay(api.Synchronize(2));
  Record("other_stream_no_credit", ledger, backend, 16 * kMiB, 2);
  Okay(api.Free(pointers[1], Release::kAsync, 2));
  Okay(api.Synchronize(1));
  Record("one_stream_complete", ledger, backend, 8 * kMiB, 1);
  void* reuse = nullptr;
  Okay(api.Allocate(Kind::kAsyncDevice, 8 * kMiB, &reuse, 1));
  Verify(reuse, 8 * kMiB, 73, backend.streams[0]);
  std::printf("{\"event\":\"address_reuse_observation\",\"same\":%s}\n",
              reuse == pointers[0] || reuse == pointers[1] ? "true" : "false");
  Okay(api.Free(reuse, Release::kAsync, 1));
  Record("both_streams_pending", ledger, backend, 16 * kMiB, 2);
  Okay(api.Synchronize());
  Record("device_complete", ledger, backend, 0, 0);
  for (auto stream : backend.streams) Cuda(cudaStreamDestroy(stream));
}

void Startup() {
  namespace reserve = q4t::runtime::reservation;
  Ledger ledger(16 * kMiB);
  Backend backend;
  Allocator api(ledger, backend);
  struct StartupBackend {
    Allocator& api;
    int Allocate(size_t, size_t bytes, void** pointer) noexcept {
      return api.Allocate(Kind::kDevice, bytes, pointer).ok()
                 ? 0
                 : cudaErrorMemoryAllocation;
    }
    int Release(size_t, size_t, void* pointer,
                reserve::ReleaseReason) noexcept {
      return api.Free(pointer, quota::Release::kDevice).ok() ? 0
                                                             : cudaErrorUnknown;
    }
  } startup_backend{api};
  {
    const std::array<size_t, 3> plan{8 * kMiB, 4 * kMiB, 8 * kMiB};
    reserve::Owner<StartupBackend> owner(plan, startup_backend);
    Check(owner.Prepare().error == reserve::Error::kAllocation,
          "plan exceeds quota");
    const auto s = owner.Finalize();
    Check(s.allocated == 2 && s.freed == 2 && s.unhanded_remaining == 0 &&
              s.handed_live == 0 && s.release_unknown == 0,
          "failed plan rollback");
    Record("plan_rejected_rolled_back", ledger, backend, 0, 0);
  }
  {
    const std::array<size_t, 3> plan{4 * kMiB, 8 * kMiB, 4 * kMiB};
    reserve::Owner<StartupBackend> owner(plan, startup_backend);
    Check(owner.Prepare().ok(), "fit plan");
    std::array<void*, 3> pointers{};
    for (size_t i = 0; i < plan.size(); ++i) {
      Check(owner.Take(plan[i], &pointers[i]).ok(), "plan handoff");
      Verify(pointers[i], plan[i], static_cast<unsigned char>(i + 41));
    }
    Check(owner.Complete().ok(), "plan complete");
    Record("plan_handed_once", ledger, backend, 16 * kMiB, 3);
    Check(backend.allocations == 5, "handoff did not charge/allocate twice");
    for (void* pointer : pointers)
      Check(owner.Release(pointer).ok(), "release");
    const auto s = owner.Finalize();
    Check(s.result.ok() && s.freed == 3 && s.handed_live == 0,
          "accepted plan finalized");
    Record("plan_closed", ledger, backend, 0, 0);
  }
}

void Concurrent() {
  Ledger ledger(8 * kMiB);
  Backend backend;
  Allocator api(ledger, backend);
  std::barrier gate(9);
  std::array<quota::Result, 8> results{};
  std::array<void*, 8> pointers{};
  std::array<std::thread, 8> workers;
  for (size_t i = 0; i < workers.size(); ++i) {
    workers[i] = std::thread([&, i] {
      gate.arrive_and_wait();
      results[i] = api.Allocate(Kind::kDevice, 2 * kMiB, &pointers[i]);
      gate.arrive_and_wait();
      gate.arrive_and_wait();
      if (results[i].ok()) Okay(api.Free(pointers[i], Release::kDevice));
    });
  }
  gate.arrive_and_wait();
  gate.arrive_and_wait();
  int accepted = 0;
  for (size_t i = 0; i < workers.size(); ++i) {
    if (results[i].ok()) {
      ++accepted;
      Verify(pointers[i], 2 * kMiB, static_cast<unsigned char>(i + 81));
    } else {
      Check(results[i].error == quota::Error::kByteLimit && !pointers[i],
            "concurrent rejection");
    }
  }
  Check(accepted == 4 && backend.allocations == 4, "concurrent exact cap");
  Record("concurrent_held", ledger, backend, 8 * kMiB, 4);
  gate.arrive_and_wait();
  for (auto& worker : workers) worker.join();
  Record("concurrent_closed", ledger, backend, 0, 0);
}
}  // namespace

int main(int argc, char** argv) {
  Check(argc == 3, "mode and parent PID required");
  pid_t expected_parent = 0;
  const auto parsed =
      std::from_chars(argv[2], argv[2] + std::strlen(argv[2]), expected_parent);
  Check(parsed.ec == std::errc{} && *parsed.ptr == '\0' && expected_parent > 1,
        "parent PID");
  Check(prctl(PR_SET_PDEATHSIG, SIGKILL) == 0 && getppid() == expected_parent,
        "parent-death binding");
  int runtime = 0, driver = 0;
  Cuda(cudaRuntimeGetVersion(&runtime));
  Cuda(cudaDriverGetVersion(&driver));
  cudaDeviceProp prop{};
  Cuda(cudaGetDeviceProperties(&prop, 0));
  Check(prop.major == 11 && prop.minor == 0 && prop.integrated,
        "Thor integrated SM110");
  std::printf(
      "{\"event\":\"device\",\"runtime\":%d,\"driver\":%d,"
      "\"sm_major\":%d,\"sm_minor\":%d,\"integrated\":%d}\n",
      runtime, driver, prop.major, prop.minor, prop.integrated);
  if (!std::strcmp(argv[1], "mixed"))
    Mixed();
  else if (!std::strcmp(argv[1], "async"))
    Async();
  else if (!std::strcmp(argv[1], "startup"))
    Startup();
  else if (!std::strcmp(argv[1], "concurrent"))
    Concurrent();
  else
    Check(false, "unknown mode");
  Cuda(cudaDeviceSynchronize());
  std::printf("{\"event\":\"result\",\"mode\":\"%s\",\"passed\":true}\n",
              argv[1]);
}
