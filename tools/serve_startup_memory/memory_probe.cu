// Bounded allocation accounting probe. Never a physical RAM limit or benchmark.
#include <cuda_runtime.h>
#include <fcntl.h>
#include <sys/mman.h>
#include <unistd.h>

#include <chrono>
#include <cstdint>
#include <cstdio>
#include <filesystem>
#include <stdexcept>
#include <string>
#include <thread>

namespace {
constexpr size_t kBytes = size_t{256} << 20;
constexpr size_t kWords = kBytes / sizeof(uint32_t);
constexpr uint32_t kMask = 0x5a5a5a5a;

void Check(cudaError_t status) {
  if (status != cudaSuccess) throw std::runtime_error(cudaGetErrorString(status));
}

__global__ void Touch(uint32_t* data, size_t words) {
  for (size_t i = blockIdx.x * blockDim.x + threadIdx.x; i < words;
       i += size_t{gridDim.x} * blockDim.x)
    data[i] = static_cast<uint32_t>(i) ^ kMask;
}

void CpuTouch(void* pointer) {
  auto* data = static_cast<volatile uint32_t*>(pointer);
  for (size_t i = 0; i < kWords; ++i)
    data[i] = static_cast<uint32_t>(i) ^ kMask;
  if (data[0] != kMask ||
      data[kWords - 1] != (static_cast<uint32_t>(kWords - 1) ^ kMask))
    throw std::runtime_error("host payload verification failed");
}

void GpuTouch(void* pointer) {
  auto* data = static_cast<uint32_t*>(pointer);
  Touch<<<256, 256>>>(data, kWords);
  Check(cudaGetLastError());
  Check(cudaDeviceSynchronize());
  uint32_t first = 0, last = 0;
  Check(cudaMemcpy(&first, data, sizeof(first), cudaMemcpyDeviceToHost));
  Check(cudaMemcpy(&last, data + kWords - 1, sizeof(last),
                   cudaMemcpyDeviceToHost));
  if (first != kMask || last != (static_cast<uint32_t>(kWords - 1) ^ kMask))
    throw std::runtime_error("device payload verification failed");
}

void Publish(const std::filesystem::path& path, const std::string& text) {
  const std::string temporary = path.string() + ".tmp";
  const int fd = open(temporary.c_str(), O_WRONLY | O_CREAT | O_EXCL, 0600);
  if (fd < 0) throw std::runtime_error("probe receipt open failed");
  const bool okay = write(fd, text.data(), text.size()) ==
                        static_cast<ssize_t>(text.size()) && fsync(fd) == 0;
  close(fd);
  if (!okay || rename(temporary.c_str(), path.c_str()) != 0)
    throw std::runtime_error("probe receipt publication failed");
}

void Checkpoint(const std::filesystem::path& folder, const std::string& phase,
                const std::string& kind, const std::string& touch) {
  size_t free = 0, total = 0;
  Check(cudaMemGetInfo(&free, &total));
  Publish(folder / (phase + ".json"),
          "{\"kind\":\"" + kind + "\",\"phase\":\"" + phase +
              "\",\"bytes\":" + std::to_string(kBytes) +
              ",\"touch\":\"" + touch + "\",\"cuda_free_bytes\":" +
              std::to_string(free) + ",\"cuda_total_bytes\":" +
              std::to_string(total) + "}\n");
  const auto deadline = std::chrono::steady_clock::now() +
                        std::chrono::seconds(30);
  const auto acknowledge = folder / (phase + ".continue");
  while (!std::filesystem::exists(acknowledge)) {
    if (std::chrono::steady_clock::now() >= deadline)
      throw std::runtime_error("probe observation acknowledgement timed out");
    std::this_thread::sleep_for(std::chrono::milliseconds(10));
  }
}

struct Allocation {
  std::string kind;
  void* pointer = nullptr;
  void Release() {
    void* current = pointer;
    pointer = nullptr;  // Never retry an allocation release of unknown outcome.
    if (!current) return;
    if (kind == "anonymous") {
      if (munmap(current, kBytes)) throw std::runtime_error("munmap failed");
    } else if (kind == "pinned") {
      Check(cudaFreeHost(current));
    } else {
      Check(cudaFree(current));
    }
  }
  ~Allocation() {
    try {
      Release();
    } catch (const std::exception& error) {
      std::fprintf(stderr, "probe cleanup failed: %s\n", error.what());
    }
  }
};
}  // namespace

int main(int argc, char** argv) {
  try {
    if (argc != 3) throw std::runtime_error("KIND EXISTING_OUTPUT_DIR required");
    const std::string kind = argv[1];
    const std::filesystem::path folder(argv[2]);
    if ((kind != "anonymous" && kind != "device" && kind != "pinned" &&
         kind != "managed") || !folder.is_absolute() ||
        !std::filesystem::is_directory(folder))
      throw std::runtime_error("invalid bounded probe arguments");
    Check(cudaSetDevice(0));
    Check(cudaFree(nullptr));
    int driver = 0, runtime = 0;
    Check(cudaDriverGetVersion(&driver));
    Check(cudaRuntimeGetVersion(&runtime));
    cudaDeviceProp properties{};
    Check(cudaGetDeviceProperties(&properties, 0));
    Publish(folder / "device.json",
            "{\"driver\":" + std::to_string(driver) +
                ",\"runtime\":" + std::to_string(runtime) +
                ",\"major\":" + std::to_string(properties.major) +
                ",\"minor\":" + std::to_string(properties.minor) +
                ",\"integrated\":" + std::to_string(properties.integrated) +
                ",\"pageable_memory_access\":" +
                std::to_string(properties.pageableMemoryAccess) +
                ",\"concurrent_managed_access\":" +
                std::to_string(properties.concurrentManagedAccess) + "}\n");
    Checkpoint(folder, "baseline", kind, "context_initialized");
    Allocation allocation{kind, nullptr};
    if (kind == "anonymous") {
      allocation.pointer = mmap(nullptr, kBytes, PROT_READ | PROT_WRITE,
                                MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
      if (allocation.pointer == MAP_FAILED) {
        allocation.pointer = nullptr;
        throw std::runtime_error("mmap failed");
      }
      CpuTouch(allocation.pointer);
    } else if (kind == "device") {
      Check(cudaMalloc(&allocation.pointer, kBytes));
      GpuTouch(allocation.pointer);
    } else if (kind == "pinned") {
      Check(cudaMallocHost(&allocation.pointer, kBytes));
      CpuTouch(allocation.pointer);
    } else {
      Check(cudaMallocManaged(&allocation.pointer, kBytes));
      CpuTouch(allocation.pointer);
      Checkpoint(folder, "host", kind, "cpu_all_words_verified_edges");
      GpuTouch(allocation.pointer);
    }
    const std::string touch = kind == "device" || kind == "managed"
                                  ? "gpu_all_words_verified_edges"
                                  : "cpu_all_words_verified_edges";
    Checkpoint(folder, "held", kind, touch);
    allocation.Release();
    Check(cudaDeviceSynchronize());
    Checkpoint(folder, "freed", kind, "release_returned_success");
    Publish(folder / "result.json", "{\"passed\":true,\"bytes\":" +
                std::to_string(kBytes) + ",\"allocations\":1,\"releases\":1}\n");
    return 0;
  } catch (const std::exception& error) {
    std::fprintf(stderr, "memory probe failed: %s\n", error.what());
    return 1;
  }
}
