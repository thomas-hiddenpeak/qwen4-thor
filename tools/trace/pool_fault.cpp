// Test-only real CUDA OOM and partial pinned-pool ownership checks.
#include <cuda_runtime_api.h>
#include <dlfcn.h>
#include <array>
#include <atomic>
#include <cstdio>
#include <cstdlib>
#include <cstring>

namespace {
constexpr size_t kPoolBytes = 48 * 8192 * 10 * sizeof(int);
std::atomic<bool> fired{false};
std::array<void*, 4> pinned{};
int pinned_count = 0;
template <typename T>
T Resolve(const char* name) {
  auto result = reinterpret_cast<T>(dlsym(RTLD_NEXT, name));
  if (!result) std::_Exit(127);
  return result;
}
cudaError_t Fail(void** pointer) {
  static const auto real = Resolve<decltype(&cudaMalloc)>("cudaMalloc");
  void* impossible = nullptr;
  const auto error = real(&impossible, size_t{1} << 60);
  std::fprintf(stderr, "[trace-pool-fault] real_error=%d last=%d\n", int(error),
               int(cudaPeekAtLastError()));
  if (error != cudaErrorMemoryAllocation || impossible) std::_Exit(126);
  *pointer = nullptr;
  return error;
}
__attribute__((destructor)) void Report() {
  int live = 0;
  for (auto pointer : pinned) live += pointer != nullptr;
  if (fired) std::fprintf(stderr, "[trace-pool-fault] pinned_live=%d\n", live);
  if (live != 0) std::_Exit(125);
}
}  // namespace
extern "C" cudaError_t CUDARTAPI cudaMalloc(void** pointer, size_t bytes) {
  static const auto real = Resolve<decltype(&cudaMalloc)>("cudaMalloc");
  const char* mode = std::getenv("Q4T_TEST_TRACE_ALLOC");
  if (mode && std::strcmp(mode, "device") == 0 && bytes == kPoolBytes &&
      !fired.exchange(true))
    return Fail(pointer);
  return real(pointer, bytes);
}
extern "C" cudaError_t CUDARTAPI cudaMallocHost(void** pointer, size_t bytes) {
  static const auto real = Resolve<decltype(&cudaMallocHost)>("cudaMallocHost");
  const char* mode = std::getenv("Q4T_TEST_TRACE_ALLOC");
  if (mode && std::strcmp(mode, "pinned") == 0 && bytes == kPoolBytes) {
    if (pinned_count == 2 && !fired.exchange(true)) return Fail(pointer);
    const auto result = real(pointer, bytes);
    if (result == cudaSuccess && pinned_count < 4)
      pinned[pinned_count++] = *pointer;
    return result;
  }
  return real(pointer, bytes);
}
extern "C" cudaError_t CUDARTAPI cudaFreeHost(void* pointer) {
  static const auto real = Resolve<decltype(&cudaFreeHost)>("cudaFreeHost");
  const auto result = real(pointer);
  if (result == cudaSuccess)
    for (auto& owned : pinned)
      if (owned == pointer) owned = nullptr;
  return result;
}
