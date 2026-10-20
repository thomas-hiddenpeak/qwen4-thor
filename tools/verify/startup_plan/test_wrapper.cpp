// Host-only executable: every __real_* CUDA operation below is a CPU fake.
#include <cuda_runtime_api.h>

#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <unordered_set>
#include <vector>
#include "q4t/model/model.h"
#include "q4t/runtime/startup_plan.h"
#include "q4t/server/server_options.h"

extern "C" cudaError_t __wrap_cudaMalloc(void**, size_t);
extern "C" cudaError_t __wrap_cudaFree(void*);
extern "C" cudaError_t __wrap_cudaHostAlloc(void**, size_t, unsigned int);
extern "C" cudaError_t __wrap_cudaFreeHost(void*);

namespace {
std::unordered_set<void*> live;
size_t allocations = 0;
std::vector<size_t> physical_sizes;
std::vector<void*> physical_pointers;
std::vector<void*> freed_pointers;
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
  physical_sizes.push_back(bytes);
  if (std::strcmp(mode, "allocation_failure") == 0 && allocations == 4) {
    *pointer = nullptr;
    return cudaErrorMemoryAllocation;
  }
  *pointer = std::malloc(bytes);
  Check(*pointer != nullptr && live.insert(*pointer).second);
  physical_pointers.push_back(*pointer);
  return cudaSuccess;
}

extern "C" cudaError_t __real_cudaFree(void* pointer) {
  if (pointer) {
    Check(live.erase(pointer) == 1);
    freed_pointers.push_back(pointer);
    std::free(pointer);
  }
  return cudaSuccess;
}

extern "C" cudaError_t __real_cudaHostAlloc(void** pointer, size_t bytes,
                                            unsigned int) {
  Check(allocations >= 4);
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

namespace q4t::runtime {
Status StartupPlan::AddBytes(const std::string& name, size_t bytes) {
  entries_.push_back({name, bytes});
  device_bytes_ += bytes;
  return Status();
}
Status BuildStartupPlan(const model::ModelConfig&, const server::ServerOptions&,
                        StartupPlan* plan) {
  size_t index = 0;
  for (size_t size : {size_t{16}, size_t{32}, size_t{48}, size_t{32}}) {
    plan->AddBytes(std::to_string(index++), size);
  }
  return Status();
}
}  // namespace q4t::runtime
namespace q4t::quant {
int MoEResidencyL2Slots() { return 16; }
int MoEResidencyMirrorK() { return 8; }
int MoEResidencyLoadThreads() { return 16; }
}  // namespace q4t::quant

int main(int argc, char** argv) {
  Check(argc == 2);
  mode = argv[1];
  using q4t::runtime::CompleteStartupReservation;
  void* pointer = nullptr;
  if (std::strcmp(mode, "early_pinned") == 0) {
    __wrap_cudaHostAlloc(&pointer, 8, 0);
    std::abort();
  }
  if (std::strcmp(mode, "retry_output_failure") == 0) {
    const std::string output = std::getenv("Q4T_STARTUP_PLAN_OUTPUT");
    const std::string invalid = output + "/missing/file";
    setenv("Q4T_STARTUP_PLAN_OUTPUT", invalid.c_str(), 1);
    Check(!q4t::runtime::PrepareStartupReservation(q4t::model::ModelConfig{},
                                                   q4t::server::ServerOptions{})
               .ok());
    Check(allocations == 0 && live.empty());
    setenv("Q4T_STARTUP_PLAN_OUTPUT", output.c_str(), 1);
  }
  const auto prepared = q4t::runtime::PrepareStartupReservation(
      q4t::model::ModelConfig{}, q4t::server::ServerOptions{});
  if (std::strcmp(mode, "allocation_failure") == 0) {
    Check(!prepared.ok() && live.empty());
    Check(physical_sizes == std::vector<size_t>{48, 32, 32, 16});
    Check(freed_pointers == std::vector<void*>{physical_pointers[2],
                                             physical_pointers[1],
                                             physical_pointers[0]});
    return 0;
  }
  Check(prepared.ok() && allocations == 4);
  Check(physical_sizes == std::vector<size_t>{48, 32, 32, 16});
  if (std::strcmp(mode, "mismatch") == 0) {
    Check(__wrap_cudaMalloc(&pointer, 32) == cudaErrorInvalidValue);
    return 0;
  }
  void* device[4]{};
  Check(__wrap_cudaMalloc(&device[0], 16) == cudaSuccess);
  Check(device[0] == physical_pointers[3]);
  if (std::strcmp(mode, "partial") == 0) {
    Check(!CompleteStartupReservation().ok());
    Check(__wrap_cudaFree(device[0]) == cudaSuccess);
    return 0;
  }
  void* host = nullptr;
  Check(__wrap_cudaHostAlloc(&host, 8, 0) == cudaSuccess);
  Check(__wrap_cudaMalloc(&device[1], 32) == cudaSuccess);
  Check(__wrap_cudaMalloc(&device[2], 48) == cudaSuccess);
  Check(__wrap_cudaMalloc(&device[3], 32) == cudaSuccess);
  Check(device[1] == physical_pointers[1] &&
        device[2] == physical_pointers[0] &&
        device[3] == physical_pointers[2]);
  if (std::strcmp(mode, "freed_before_ready") == 0) {
    Check(__wrap_cudaFree(device[0]) == cudaSuccess);
    Check(!CompleteStartupReservation().ok());
    for (int i = 1; i < 4; ++i)
      Check(__wrap_cudaFree(device[i]) == cudaSuccess);
    Check(__wrap_cudaFreeHost(host) == cudaSuccess);
    return 0;
  }
  if (std::strcmp(mode, "extra_before_ready") == 0) {
    Check(__wrap_cudaMalloc(&pointer, 16) == cudaErrorInvalidValue);
  } else {
    Check(CompleteStartupReservation().ok());
    std::fputs("serving on port 1\n", stderr);
  }
  for (void* block : device) Check(__wrap_cudaFree(block) == cudaSuccess);
  Check(__wrap_cudaFreeHost(host) == cudaSuccess);
  if (std::strcmp(mode, "extra_before_ready") == 0) return 0;
  for (size_t bytes : {size_t{16}, size_t{32}}) {
    Check(__wrap_cudaMalloc(&pointer, bytes) == cudaSuccess);
    Check(__wrap_cudaFree(pointer) == cudaSuccess);
  }
  Check(allocations == 6 && live.empty());
  return 0;
}
