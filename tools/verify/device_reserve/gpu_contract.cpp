// Small real-CUDA owner contract. Run only under the whole-stage GPU lease.
#include <cuda_runtime_api.h>

#include <algorithm>
#include <array>
#include <cstdio>
#include <stdexcept>
#include <vector>

#include "owner.h"

namespace reserve = q4t::verify::device_reserve;

namespace {
void Check(bool condition, const char* message) {
  if (!condition) throw std::runtime_error(message);
}

struct Backend {
  int allocations = 0;
  int frees = 0;
  int Allocate(size_t, size_t bytes, void** pointer) {
    const auto result = cudaMalloc(pointer, bytes);
    if (result == cudaSuccess) ++allocations;
    return int(result);
  }
  int Release(size_t, size_t, void* pointer, reserve::ReleaseReason) {
    const auto result = cudaFree(pointer);
    if (result == cudaSuccess) ++frees;
    return int(result);
  }
};

void ReserveReadWriteRelease() {
  Backend backend;
  const std::array<size_t, 2> plan{(1u << 20) + 1, (2u << 20) + 17};
  reserve::Owner owner(plan, backend);
  Check(owner.Prepare().ok() && backend.allocations == 2, "reserve all");
  for (size_t bytes : plan) {
    void* pointer = nullptr;
    Check(owner.Take(bytes, &pointer).ok(), "handoff");
    Check(cudaMemset(pointer, 0x5a, bytes) == cudaSuccess, "device memset");
    std::vector<unsigned char> output(bytes);
    Check(cudaMemcpy(output.data(), pointer, bytes, cudaMemcpyDeviceToHost) ==
              cudaSuccess, "device readback");
    Check(std::all_of(output.begin(), output.end(),
                      [](unsigned char value) { return value == 0x5a; }),
          "readback mismatch");
    Check(owner.Release(pointer).ok(), "release");
  }
  const auto done = owner.Finalize();
  Check(done.result.ok() && done.freed == 2 && backend.frees == 2,
        "complete lifecycle");
}

void MismatchAndUnusedRollback() {
  Backend backend;
  const std::array<size_t, 2> plan{4096, 8192};
  reserve::Owner owner(plan, backend);
  Check(owner.Prepare().ok(), "mismatch preparation");
  void* pointer = nullptr;
  Check(owner.Take(8192, &pointer).error == reserve::Error::kSizeMismatch,
        "size mismatch not rejected");
  const auto done = owner.Finalize();
  Check(!done.result.ok() && done.consumed == 0 && done.freed == 2 &&
            done.unhanded_remaining == 0 && backend.frees == 2,
        "mismatch cleanup");
}

void PartialHandoffAndUnusedCleanup() {
  Backend backend;
  const std::array<size_t, 2> plan{4096, 8192};
  reserve::Owner owner(plan, backend);
  Check(owner.Prepare().ok(), "partial preparation");
  void* pointer = nullptr;
  Check(owner.Take(4096, &pointer).ok(), "partial handoff");
  Check(owner.Release(pointer).ok(), "partial application free");
  const auto done = owner.Finalize();
  Check(done.result.error == reserve::Error::kPlanIncomplete &&
            done.freed == 2 && done.handed_live == 0 && backend.frees == 2,
        "incomplete plan or unused cleanup");
}
}  // namespace

int main() {
  try {
    Check(cudaFree(nullptr) == cudaSuccess, "CUDA context initialization");
    ReserveReadWriteRelease();
    MismatchAndUnusedRollback();
    PartialHandoffAndUnusedCleanup();
  } catch (const std::exception& error) {
    std::fprintf(stderr, "device reserve GPU contract FAILED: %s\n",
                 error.what());
    return 1;
  }
  std::puts("{\"status\":\"PASS\",\"gpu_contracts\":3,"
            "\"largest_plan_bytes\":3145746}");
}
