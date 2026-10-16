#include <cuda_runtime.h>
#include <chrono>
#include <cstdio>
#include <cstdlib>
#include <iostream>
#include <string>
#include <thread>
#include <vector>
void Check(cudaError_t error) {
  if (error != cudaSuccess) {
    std::fprintf(stderr, "%s\n", cudaGetErrorString(error));
    std::exit(1);
  }
}
__global__ void Copy(const uint4* input, uint4* output, size_t count) {
  for (size_t i = blockIdx.x * blockDim.x + threadIdx.x;
       i < count; i += size_t{blockDim.x} * gridDim.x) {
    output[i] = input[i];
  }
}
int main() {
  constexpr size_t kBytes = 256ULL * 1024 * 1024;
  uint4* input = nullptr;
  uint4* output = nullptr;
  Check(cudaMalloc(&input, kBytes));
  Check(cudaMalloc(&output, kBytes));
  Check(cudaMemset(input, 0x5a, kBytes));
  Check(cudaMemset(output, 0, kBytes));
  // Resolve the kernel before any counting window, without traversing arrays.
  Copy<<<1, 1>>>(input, output, 1);
  Check(cudaGetLastError());
  Check(cudaDeviceSynchronize());
  std::cout << "ready" << std::endl;
  for (const std::string expected : {"idle", "one", "four"}) {
    std::string command;
    if (!std::getline(std::cin, command) || command != expected) return 2;
    const int count = command == "idle" ? 0 : command == "one" ? 1 : 4;
    if (!count) std::this_thread::sleep_for(std::chrono::milliseconds(20));
    for (int i = 0; i < count; ++i) {
      Copy<<<4096, 256>>>(input, output, kBytes / sizeof(uint4));
      Check(cudaGetLastError());
    }
    Check(cudaDeviceSynchronize());
    std::cout << "done" << std::endl;
    if (!std::getline(std::cin, command) || command != "verify") return 2;
    if (count) {
      std::vector<unsigned char> host(kBytes);
      Check(cudaMemcpy(host.data(), output, kBytes, cudaMemcpyDeviceToHost));
      for (unsigned char value : host) if (value != 0x5a) return 3;
    }
    std::cout << "verified" << std::endl;
  }
  Check(cudaFree(output));
  Check(cudaFree(input));
}
