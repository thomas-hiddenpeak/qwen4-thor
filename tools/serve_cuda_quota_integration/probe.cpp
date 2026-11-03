// Small real CUDA checks through the production GNU link wrappers.
#include <cuda_runtime_api.h>
#include <signal.h>
#include <sys/prctl.h>
#include <unistd.h>

#include <algorithm>
#include <array>
#include <charconv>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <vector>

#include "q4t/runtime/cuda_allocation_quota.h"
#include "q4t/runtime/startup_plan.h"
#include "q4t/runtime/startup_reservation.h"

namespace {
namespace rt = q4t::runtime;
constexpr size_t kMiB = 1024 * 1024;
size_t verified = 0, verified_bytes = 0, checkpoints = 0;
void Check(bool value, const char* reason) {
  if (!value) {
    std::fprintf(stderr, "FAIL: %s\n", reason);
    std::_Exit(1);
  }
}
void Cuda(cudaError_t error) {
  if (error != cudaSuccess) {
    std::fprintf(stderr, "CUDA %d: %s\n", int(error),
                 cudaGetErrorString(error));
    std::_Exit(1);
  }
}
void Configure(uint64_t limit) {
  Check(rt::ConfigureCudaAllocationQuota(limit).ok(), "configure");
}
void Record(const char* label, uint64_t charged, uint64_t pending = 0) {
  const auto s = rt::ReadCudaAllocationQuota();
  const auto& m = s.memory;
  Check(m.charged_bytes == charged && m.pending_bytes == pending &&
            !m.reserved_bytes && !m.releasing_bytes && !m.unknown_bytes &&
            !m.poisoned && m.live_bytes + m.pending_bytes == charged &&
            (!s.enabled || m.peak_bytes <= m.limit_bytes),
        label);
  ++checkpoints;
  std::printf(
      "{\"event\":\"quota\",\"label\":\"%s\",\"enabled\":%s,"
      "\"charged\":%llu,\"pending\":%llu,\"live\":%llu,\"peak\":%llu,"
      "\"limit\":%llu,\"entries\":%zu,\"byte_rejections\":%llu,"
      "\"stream_entries\":%zu,\"unknown\":%llu,\"poisoned\":false}\n",
      label, s.enabled ? "true" : "false",
      static_cast<unsigned long long>(charged),
      static_cast<unsigned long long>(pending),
      static_cast<unsigned long long>(m.live_bytes),
      static_cast<unsigned long long>(m.peak_bytes),
      static_cast<unsigned long long>(m.limit_bytes), m.entries,
      static_cast<unsigned long long>(s.byte_rejections), s.stream_entries,
      static_cast<unsigned long long>(m.unknown_bytes));
}
void Verify(void* pointer, size_t bytes, int pattern,
            cudaStream_t stream = nullptr, bool host = false) {
  if (host) {
    std::memset(pointer, pattern, bytes);
    const auto* p = static_cast<const unsigned char*>(pointer);
    Check(std::all_of(p, p + bytes, [=](auto v) { return v == pattern; }),
          "full pinned host contents");
  } else {
    Cuda(cudaMemsetAsync(pointer, pattern, bytes, stream));
    std::vector<unsigned char> actual(bytes);
    Cuda(cudaMemcpyAsync(actual.data(), pointer, bytes, cudaMemcpyDeviceToHost,
                         stream));
    Cuda(cudaStreamSynchronize(stream));
    Check(std::all_of(actual.begin(), actual.end(),
                      [=](auto v) { return v == pattern; }),
          "full device data");
  }
  ++verified;
  verified_bytes += bytes;
  std::printf(
      "{\"event\":\"verified\",\"bytes\":%zu,\"pattern\":%d,"
      "\"host\":%s,\"full_buffer\":true}\n",
      bytes, pattern, host ? "true" : "false");
}
rt::StartupPlan Plan(std::initializer_list<size_t> sizes) {
  rt::StartupPlan plan;
  for (auto size : sizes)
    Check(plan.AddBytes(std::to_string(plan.entries().size()), size).ok(),
          "construct plan");
  return plan;
}
void Mixed() {
  Configure(32 * kMiB);
  std::array<void*, 4> p{};
  Cuda(cudaMalloc(&p[0], 8 * kMiB));
  Cuda(cudaHostAlloc(&p[1], 8 * kMiB, cudaHostAllocPortable));
  unsigned flags = 0;
  Cuda(cudaHostGetFlags(&flags, p[1]));
  Check(flags == cudaHostAllocPortable, "host flags preserved");
  Cuda(cudaMallocHost(&p[2], 8 * kMiB));
  Cuda(cudaMallocManaged(&p[3], 8 * kMiB, cudaMemAttachGlobal));
  for (int i = 0; i < 4; ++i)
    Verify(p[i], 8 * kMiB, 31 + i, nullptr, i == 1 || i == 2);
  Record("mixed_full", 32 * kMiB);
  void* refused = nullptr;
  Check(cudaMalloc(&refused, 1) == cudaErrorMemoryAllocation && !refused &&
            rt::LastCudaQuotaRejection() == rt::CudaQuotaRejection::kBytes,
        "quota denial");
  void* zero = reinterpret_cast<void*>(1);
  Cuda(cudaMalloc(&zero, 0));
  Check(!zero, "zero-byte pointer");
  Cuda(cudaFree(nullptr));
  Cuda(cudaFree(p[0]));
  Cuda(cudaFreeHost(p[1]));
  Cuda(cudaFreeHost(p[2]));
  Cuda(cudaFree(p[3]));
  Record("mixed_closed", 0);
}
void Async(bool destroy) {
  Configure(16 * kMiB);
  cudaStream_t first = nullptr, other = nullptr;
  Cuda(cudaStreamCreateWithFlags(&first, cudaStreamNonBlocking));
  Cuda(cudaStreamCreateWithPriority(&other, cudaStreamNonBlocking, 0));
  void* a = nullptr;
  void* b = nullptr;
  Cuda(cudaMallocAsync(&a, 8 * kMiB, first));
  Cuda(cudaMallocAsync(&b, 8 * kMiB, other));
  Verify(a, 8 * kMiB, 61, first);
  Verify(b, 8 * kMiB, 62, other);
  Cuda(cudaFreeAsync(a, first));
  Record("async_pending", 16 * kMiB, 8 * kMiB);
  Cuda(cudaStreamSynchronize(other));
  Record("other_no_credit", 16 * kMiB, 8 * kMiB);
  if (destroy) {
    const auto old = first;
    Cuda(cudaStreamDestroy(first));
    Cuda(cudaStreamCreate(&first));
    std::printf("{\"event\":\"handle_reuse\",\"observed\":%s}\n",
                old == first ? "true" : "false");
    Cuda(cudaStreamSynchronize(first));
    Record("new_generation_no_credit", 16 * kMiB, 8 * kMiB);
  } else {
    Cuda(cudaStreamSynchronize(first));
    Record("matching_credit", 8 * kMiB);
  }
  Cuda(cudaFreeAsync(b, other));
  Cuda(cudaDeviceSynchronize());
  Record("device_closed", 0);
  Cuda(cudaStreamDestroy(first));
  Cuda(cudaStreamDestroy(other));
}
void Startup(bool reject) {
  Configure(16 * kMiB);
  rt::StartupReservation owner;
  if (reject) {
    Check(!owner.Prepare(Plan({8 * kMiB, 8 * kMiB, 4 * kMiB})).ok(),
          "startup rejection");
    Check(owner.Finalize().ownership.freed == 2, "partial rollback count");
    Check(rt::ReadCudaAllocationQuota().byte_rejections == 1, "quota refusal");
    Record("startup_rollback", 0);
    return;
  }
  Check(owner.Prepare(Plan({4 * kMiB, 8 * kMiB, 4 * kMiB})).ok(), "prepare");
  Record("startup_reserved", 16 * kMiB);
  std::array<void*, 3> p{};
  for (int i = 0; i < 3; ++i) {
    const auto bytes = (i == 1 ? 8 : 4) * kMiB;
    Cuda(cudaMalloc(&p[i], bytes));
    Verify(p[i], bytes, 91 + i);
  }
  Check(owner.Complete().ok(), "handoff complete");
  Record("startup_handed_once", 16 * kMiB);
  for (void* pointer : p) Cuda(cudaFree(pointer));
  Check(owner.Finalize().ownership.result.ok(), "finalize");
  Record("startup_closed", 0);
}
void Disabled() {
  Configure(0);
  void* pointer = nullptr;
  Cuda(cudaMalloc(&pointer, 8 * kMiB));
  Verify(pointer, 8 * kMiB, 127);
  Record("explicitly_disabled", 0);
  Cuda(cudaFree(pointer));
}
}  // namespace

int main(int argc, char** argv) {
  Check(argc == 3, "mode and expected parent required");
  int parent = 0;
  const auto parsed =
      std::from_chars(argv[2], argv[2] + std::strlen(argv[2]), parent);
  Check(parsed.ec == std::errc{} && *parsed.ptr == '\0' && parent > 1 &&
            getppid() == parent && prctl(PR_SET_PDEATHSIG, SIGKILL) == 0 &&
            getppid() == parent,
        "parent identity binding");
  setvbuf(stdout, nullptr, _IOLBF, 0);
  cudaDeviceProp prop{};
  Cuda(cudaGetDeviceProperties(&prop, 0));
  Check(prop.major == 11 && prop.minor == 0 && prop.integrated,
        "Thor integrated SM110");
  int runtime = 0, driver = 0;
  Cuda(cudaRuntimeGetVersion(&runtime));
  Cuda(cudaDriverGetVersion(&driver));
  std::printf(
      "{\"event\":\"device\",\"runtime\":%d,\"driver\":%d,"
      "\"sm_major\":%d,\"sm_minor\":%d,\"integrated\":%d}\n",
      runtime, driver, prop.major, prop.minor, prop.integrated);
  if (!std::strcmp(argv[1], "mixed"))
    Mixed();
  else if (!std::strcmp(argv[1], "async"))
    Async(false);
  else if (!std::strcmp(argv[1], "stream_reuse"))
    Async(true);
  else if (!std::strcmp(argv[1], "startup"))
    Startup(false);
  else if (!std::strcmp(argv[1], "startup_reject"))
    Startup(true);
  else if (!std::strcmp(argv[1], "disabled"))
    Disabled();
  else
    Check(false, "mode");
  Cuda(cudaDeviceSynchronize());
  rt::ReportCudaAllocationQuota("native_end");
  std::printf(
      "{\"event\":\"result\",\"mode\":\"%s\",\"passed\":true,"
      "\"verified_buffers\":%zu,\"verified_bytes\":%zu,"
      "\"checkpoints\":%zu}\n",
      argv[1], verified, verified_bytes, checkpoints);
}
