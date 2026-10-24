// Known-work capture contract; this is not an inference performance benchmark.
#include <cuda_profiler_api.h>
#include <cuda_runtime.h>
#include <nvtx3/nvToolsExt.h>
#include <sys/syscall.h>
#include <unistd.h>

#include <chrono>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <filesystem>
#include <thread>

namespace {
constexpr int kIterations = 32;
constexpr int kElements = 4096;
constexpr int kBytes = kElements * sizeof(uint32_t);

__global__ void K3FixtureKernel(uint32_t* values, int iteration) {
  const int index = static_cast<int>(blockIdx.x * blockDim.x + threadIdx.x);
  if (index < kElements) values[index] += static_cast<uint32_t>(7 + iteration);
}

bool Check(cudaError_t result, const char* operation) {
  if (result == cudaSuccess) return true;
  std::fprintf(stderr,
      "[fixture-error] {\"operation\":\"%s\",\"cuda_error\":%d}\n",
      operation, static_cast<int>(result));
  return false;
}

struct Buffers {
  uint32_t* input = nullptr;
  uint32_t* output = nullptr;
  uint32_t* device = nullptr;
  cudaStream_t stream = nullptr;

  bool Release() {
    bool ok = true;
    if (device) {
      ok = Check(cudaFree(device), "cudaFree") && ok;
      device = nullptr;
    }
    if (input) {
      ok = Check(cudaFreeHost(input), "cudaFreeHost_input") && ok;
      input = nullptr;
    }
    if (output) {
      ok = Check(cudaFreeHost(output), "cudaFreeHost_output") && ok;
      output = nullptr;
    }
    if (stream) {
      ok = Check(cudaStreamDestroy(stream), "cudaStreamDestroy") && ok;
      stream = nullptr;
    }
    return ok;
  }
  ~Buffers() { Release(); }
};
}  // namespace

int main(int argc, char** argv) {
  if (argc != 3 || std::strcmp(argv[1], "--release") != 0) {
    std::fprintf(stderr, "usage: fixture --release PATH\n");
    return 2;
  }
  const std::filesystem::path release(argv[2]);
  std::error_code error;
  if (std::filesystem::exists(release, error) || error) {
    std::fprintf(stderr, "[fixture-error] {\"operation\":\"release_preexists\"}\n");
    return 2;
  }
  Buffers buffers;
  const size_t host_bytes = static_cast<size_t>(kIterations) * kBytes;
  if (!Check(cudaFree(nullptr), "context_init") ||
      !Check(cudaStreamCreateWithFlags(&buffers.stream, cudaStreamNonBlocking),
             "stream_create") ||
      !Check(cudaMallocHost(reinterpret_cast<void**>(&buffers.input), host_bytes),
             "host_input_alloc") ||
      !Check(cudaMallocHost(reinterpret_cast<void**>(&buffers.output), host_bytes),
             "host_output_alloc") ||
      !Check(cudaMalloc(reinterpret_cast<void**>(&buffers.device), kBytes),
             "device_alloc")) return 1;
  for (int step = 0; step < kIterations; ++step) {
    for (int index = 0; index < kElements; ++index) {
      buffers.input[step * kElements + index] =
          static_cast<uint32_t>(step * kElements + index);
      buffers.output[step * kElements + index] = 0xffffffffU;
    }
  }
  const int pid = getpid();
  const long tid = syscall(SYS_gettid);
  std::fprintf(stderr,
      "[fixture-config] {\"schema\":1,\"pid\":%d,\"tid\":%ld,"
      "\"iterations\":32,\"elements\":4096,\"bytes_per_copy\":16384,"
      "\"expected_kernels\":32,\"expected_h2d\":32,\"expected_d2h\":32,"
      "\"expected_memset_apis\":32,\"expected_nvtx\":33,"
      "\"release_timeout_seconds\":60}\n", pid, tid);
  if (!Check(cudaProfilerStart(), "profiler_start")) return 1;
  nvtxRangePushA("k3fixture|root");
  for (int step = 0; step < kIterations; ++step) {
    char name[64];
    std::snprintf(name, sizeof(name), "k3fixture|step=%d", step);
    nvtxRangePushA(name);
    if (!Check(cudaMemcpyAsync(buffers.device,
              buffers.input + step * kElements, kBytes, cudaMemcpyHostToDevice,
              buffers.stream), "h2d")) return 1;
    const int memset_bytes = step % 2 == 0 ? 512 : kBytes;
    if (!Check(cudaMemsetAsync(buffers.device, 0, memset_bytes, buffers.stream),
               "memset")) return 1;
    K3FixtureKernel<<<16, 256, 0, buffers.stream>>>(buffers.device, step);
    if (!Check(cudaPeekAtLastError(), "kernel_launch") ||
        !Check(cudaMemcpyAsync(buffers.output + step * kElements,
              buffers.device, kBytes, cudaMemcpyDeviceToHost, buffers.stream),
              "d2h")) return 1;
    nvtxRangePop();
  }
  if (!Check(cudaStreamSynchronize(buffers.stream), "final_stream_sync")) return 1;
  nvtxRangePop();
  if (!Check(cudaProfilerStop(), "profiler_stop")) return 1;
  bool all_passed = true;
  for (int step = 0; step < kIterations; ++step) {
    uint64_t input_sum = 0, output_sum = 0, expected_sum = 0;
    int mismatches = 0;
    const int memset_bytes = step % 2 == 0 ? 512 : kBytes;
    const int cleared_elements = memset_bytes / static_cast<int>(sizeof(uint32_t));
    for (int index = 0; index < kElements; ++index) {
      const uint32_t input = static_cast<uint32_t>(step * kElements + index);
      const uint32_t expected = (index < cleared_elements ? 0U : input) +
                                static_cast<uint32_t>(7 + step);
      const uint32_t actual = buffers.output[step * kElements + index];
      input_sum += input;
      expected_sum += expected;
      output_sum += actual;
      if (actual != expected) ++mismatches;
    }
    const bool passed = mismatches == 0;
    all_passed = all_passed && passed;
    std::fprintf(stderr,
        "[fixture-step] {\"id\":%d,\"pid\":%d,\"tid\":%ld,"
        "\"input_checksum\":%llu,\"output_checksum\":%llu,"
        "\"expected_checksum\":%llu,\"mismatches\":%d,\"passed\":%s,"
        "\"memset_bytes\":%d,\"memset_error\":0,"
        "\"h2d_error\":0,\"launch_error\":0,\"d2h_error\":0}\n",
        step, pid, tid, static_cast<unsigned long long>(input_sum),
        static_cast<unsigned long long>(output_sum),
        static_cast<unsigned long long>(expected_sum), mismatches,
        passed ? "true" : "false", memset_bytes);
  }
  std::fprintf(stderr,
      "[fixture-ready] {\"pid\":%d,\"tid\":%ld,\"completed_steps\":32,"
      "\"profiler_start_error\":0,\"profiler_stop_error\":0,"
      "\"final_sync_error\":0,\"passed\":%s}\n", pid, tid,
      all_passed ? "true" : "false");
  std::fflush(stderr);
  if (!all_passed) return 1;
  const auto deadline = std::chrono::steady_clock::now() +
                        std::chrono::seconds(60);
  while (!std::filesystem::exists(release, error)) {
    if (error || std::chrono::steady_clock::now() >= deadline) {
      std::fprintf(stderr,
          "[fixture-error] {\"operation\":\"release_timeout_or_io\"}\n");
      return 2;
    }
    std::this_thread::sleep_for(std::chrono::milliseconds(20));
  }
  if (error || std::chrono::steady_clock::now() > deadline) {
    std::fprintf(stderr,
        "[fixture-error] {\"operation\":\"late_release_or_io\"}\n");
    return 2;
  }
  const bool released = buffers.Release();
  std::fprintf(stderr,
      "[fixture-exit] {\"pid\":%d,\"tid\":%ld,\"released\":%s,"
      "\"returncode\":%d}\n", pid, tid, released ? "true" : "false",
      released ? 0 : 1);
  return released ? 0 : 1;
}
