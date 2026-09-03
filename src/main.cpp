// Qwen4-Thor CLI entry point.
//
// Subcommands (Phase 1):
//   version   Print version and build info.
//   probe     Probe the target device (SM110a capabilities).
//   models    List catalogued model descriptors.
//   generate  Single greedy generation.
//   serve     OpenAI-compatible HTTP API server.
//
// This is the skeleton entry point; subcommands are implemented as their
// modules land (see docs/PHASES.md).

#include <cuda_runtime.h>

#include <cstdio>
#include <cstring>
#include <string>

namespace {

void PrintVersion() {
  std::printf("q4t (Qwen4-Thor) v0.1.0 — skeleton\n");
  std::printf("  target: Jetson AGX Thor, SM110a\n");
  std::printf("  model:  Qwen3.8-Flash-Next (qwen4_exp)\n");
}

void PrintUsage(const char* prog) {
  std::printf(
      "Usage: %s <command>\n"
      "Commands:\n"
      "  version   Print version and build info\n"
      "  probe     Probe the target device\n"
      "  models    List catalogued model descriptors\n"
      "  generate  Single greedy generation (not yet implemented)\n"
      "  serve     OpenAI-compatible HTTP API server (not yet "
      "implemented)\n",
      prog);
}

int RunProbe() {
  int device_count = 0;
  cudaError_t err = cudaGetDeviceCount(&device_count);
  if (err != cudaSuccess) {
    std::fprintf(stderr, "cudaGetDeviceCount failed: %s\n",
                 cudaGetErrorString(err));
    return 1;
  }
  if (device_count == 0) {
    std::fprintf(stderr, "No CUDA devices found.\n");
    return 1;
  }
  for (int i = 0; i < device_count; ++i) {
    cudaDeviceProp prop{};
    err = cudaGetDeviceProperties(&prop, i);
    if (err != cudaSuccess) {
      std::fprintf(stderr, "cudaGetDeviceProperties(%d) failed: %s\n", i,
                   cudaGetErrorString(err));
      return 1;
    }
    std::printf("Device %d: %s\n", i, prop.name);
    std::printf("  Compute capability: %d.%d\n", prop.major, prop.minor);
    std::printf("  SM count:           %d\n", prop.multiProcessorCount);
    std::printf("  Global memory:      %.1f GB\n",
                static_cast<double>(prop.totalGlobalMem) / (1024.0 * 1024.0 *
                                                            1024.0));
    std::printf("  L2 cache:           %.1f MB\n",
                static_cast<double>(prop.l2CacheSize) / (1024.0 * 1024.0));
    std::printf("  Shared mem/SM:      %zu KB\n",
                prop.sharedMemPerMultiprocessor / 1024);
    std::printf("  Max threads/SM:     %d\n", prop.maxThreadsPerMultiProcessor);
    int clock_khz = 0;
    if (cudaDeviceGetAttribute(&clock_khz, cudaDevAttrClockRate, i) ==
        cudaSuccess) {
      std::printf("  Clock rate:         %d MHz\n", clock_khz / 1000);
    }
  }
  return 0;
}

}  // namespace

int main(int argc, char** argv) {
  if (argc < 2) {
    PrintUsage(argv[0]);
    return 1;
  }
  const std::string cmd = argv[1];
  if (cmd == "version") {
    PrintVersion();
    return 0;
  }
  if (cmd == "probe") {
    return RunProbe();
  }
  if (cmd == "models" || cmd == "generate" || cmd == "serve") {
    std::fprintf(stderr, "'%s' is not implemented yet (see docs/PHASES.md)\n",
                 cmd.c_str());
    return 2;
  }
  std::fprintf(stderr, "Unknown command: %s\n", cmd.c_str());
  PrintUsage(argv[0]);
  return 1;
}
