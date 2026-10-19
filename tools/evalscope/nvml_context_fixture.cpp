// Minimal compute-context lifecycle, no kernel or model weights.
#include <cuda_runtime.h>
#include <poll.h>
#include <unistd.h>

#include <cstdio>

int main() {
  void* memory = nullptr;
  const cudaError_t allocated = cudaMalloc(&memory, 4);
  if (allocated != cudaSuccess) {
    std::fprintf(stderr, "cudaMalloc: %s\n", cudaGetErrorString(allocated));
    return 1;
  }
  std::puts("READY");
  std::fflush(stdout);
  pollfd input{STDIN_FILENO, POLLIN, 0};
  const int ready = poll(&input, 1, 20000);
  char command = 0;
  const bool requested = ready > 0 && (input.revents & POLLIN) &&
                         read(STDIN_FILENO, &command, 1) == 1 && command == 'q';
  const cudaError_t freed = cudaFree(memory);
  if (freed != cudaSuccess) {
    std::fprintf(stderr, "cudaFree: %s\n", cudaGetErrorString(freed));
    return 1;
  }
  return requested ? 0 : 2;
}
