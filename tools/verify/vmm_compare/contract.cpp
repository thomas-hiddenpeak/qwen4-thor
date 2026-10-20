#include <cuda_runtime_api.h>
#include <cstdio>
#include <cstdlib>
#include <cstring>

void Check(cudaError_t rc) {
  if (rc != cudaSuccess) {
    std::fprintf(stderr, "CUDA error %d\n", int(rc));
    std::exit(1);
  }
}
int main() {
  Check(cudaSetDevice(0));
  cudaStream_t stream;
  Check(cudaStreamCreateWithFlags(&stream, cudaStreamNonBlocking));
  void* p = nullptr;
  // Exercise rounded large allocation and the completion-before-free path.
  Check(cudaMalloc(&p, (32u << 20) + 1));
  unsigned char expected[64], actual[64];
  for (int i = 0; i < 64; ++i) expected[i] = (i * 19 + 11) % 256;
  auto* boundary = static_cast<unsigned char*>(p) + (2u << 20) - 32;
  Check(cudaMemcpyAsync(boundary, expected, 64, cudaMemcpyHostToDevice, stream));
  Check(cudaStreamSynchronize(stream));
  Check(cudaMemcpy(actual, boundary, 64, cudaMemcpyDeviceToHost));
  if (std::memcmp(expected, actual, 64)) return 2;
  Check(cudaMemsetAsync(p, 7, (32u << 20) + 1, stream));
  Check(cudaFree(p));
  // Keep the stream-ordered allocator and host pointers on the real runtime.
  Check(cudaMallocAsync(&p, 128, stream));
  Check(cudaMemsetAsync(p, 4, 128, stream));
  Check(cudaFreeAsync(p, stream));
  Check(cudaStreamSynchronize(stream));
  Check(cudaHostAlloc(&p, 64, cudaHostAllocDefault));
  Check(cudaFreeHost(p));
  Check(cudaMallocHost(&p, 64));
  Check(cudaFreeHost(p));
  Check(cudaStreamDestroy(stream));
  std::puts("LINK_CONTRACT PASS");
}
