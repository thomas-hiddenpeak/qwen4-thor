#include "block.h"
#include <cuda_runtime_api.h>
#include <cstdio>
#include <fstream>
#include <string>
#include <vector>
#include <ctime>

void Check(int result, const char* operation) {
  if (result == 0) return;
  std::fprintf(stderr, "FAIL %s code=%d\n", operation, result);
  std::exit(1);
}
void Record(const char* phase, size_t i, const std::string& api, size_t bytes) {
  timespec t{};
  clock_gettime(CLOCK_MONOTONIC, &t);
  std::printf("%s id=%zu api=%s bytes=%zu mono=%lld.%09ld\n", phase,
              i, api.c_str(), bytes, static_cast<long long>(t.tv_sec), t.tv_nsec);
  std::fflush(stdout);
}
void Verify(const VmmBlock& block) {
  unsigned char expected[64], actual[64];
  for (int i = 0; i < 64; ++i) expected[i] = (i * 17 + 29) % 256;
  for (size_t offset : {size_t(0), block.chunk - 32, block.size - 64}) {
    Check(cuMemcpyHtoD(block.address + offset, expected, 64), "H2D");
    Check(cuMemcpyDtoH(actual, block.address + offset, 64), "D2H");
    for (int i = 0; i < 64; ++i) if (actual[i] != expected[i]) std::exit(4);
  }
}
int main(int argc, char** argv) {
  if (argc != 2) return 2;
  Check(cudaSetDevice(0), "device");
  Check(cudaFree(nullptr), "context");
  // Real partial-construction failures before first and after second map.
  for (int fail_after : {0, 2}) {
    VmmBlock block;
    auto rc = block.Allocate(6u << 20, 0, fail_after);
    if (rc != CUDA_ERROR_OUT_OF_MEMORY || block.address || block.mapped)
      return 5;
  }
  VmmBlock trial;
  Check(trial.Allocate((4u << 20) + 1, 0), "rounded allocation");
  Verify(trial);
  Check(trial.Free(), "trial free");
  Check(trial.Free(), "idempotent free");
  std::puts("owner contracts PASS");
  struct Allocation { std::string api; void* pointer; VmmBlock block; };
  std::vector<Allocation> allocations;
  std::ifstream plan(argv[1]);
  if (!plan) return 6;
  std::string api;
  size_t bytes, total = 0;
  while (plan >> api >> bytes) {
    if (allocations.size() >= 1600 || bytes > (4ull << 30) ||
        total + bytes > (64ull << 30)) return 7;
    total += bytes;
    Allocation allocation{api, nullptr, {}};
    Record("begin", allocations.size(), api, bytes);
    if (api == "cudaMalloc" && bytes >= (32u << 20)) {
      Check(allocation.block.Allocate(bytes, 0), "VMM allocate");
      Verify(allocation.block);
    } else if (api == "cudaMalloc") {
      Check(cudaMalloc(&allocation.pointer, bytes), "cudaMalloc");
    } else if (api == "cudaHostAlloc" || api == "cudaMallocHost") {
      Check(cudaHostAlloc(&allocation.pointer, bytes, cudaHostAllocDefault),
            "host allocation");
    } else return 8;
    Record("end", allocations.size(), api, bytes);
    allocations.push_back(allocation);
  }
  Check(cudaDeviceSynchronize(), "drain");
  for (auto it = allocations.rbegin(); it != allocations.rend(); ++it) {
    if (it->block.address) Check(it->block.Free(), "VMM free");
    else if (it->api == "cudaMalloc") Check(cudaFree(it->pointer), "device free");
    else Check(cudaFreeHost(it->pointer), "host free");
  }
  std::printf("replay PASS allocations=%zu payload=%zu\n", allocations.size(), total);
}
