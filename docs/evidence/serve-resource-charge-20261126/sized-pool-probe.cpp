#include <cuda_runtime_api.h>

#include <cstdint>
#include <cstdio>
#include <stdexcept>
#include <string>

namespace {
constexpr size_t kMiB = 1U << 20;
constexpr size_t kLimit = 32 * kMiB;
constexpr size_t kBlock = 24 * kMiB;

void Check(cudaError_t error, const char* operation) {
  if (error != cudaSuccess) {
    throw std::runtime_error(std::string(operation) + ": " + cudaGetErrorName(error));
  }
}
void Fill(void* block, size_t bytes, unsigned char value, cudaStream_t stream) {
  Check(cudaMemsetAsync(block, value, bytes, stream), "write entire allocation");
  unsigned char last = 0;
  Check(cudaMemcpyAsync(&last, static_cast<char*>(block) + bytes - 1, 1,
                        cudaMemcpyDeviceToHost, stream), "read tail");
  Check(cudaStreamSynchronize(stream), "complete device writes");
  if (last != value) throw std::runtime_error("wrong device payload");
}
uint64_t Counter(cudaMemPool_t pool, cudaMemPoolAttr which) {
  uint64_t bytes = 0;
  Check(cudaMemPoolGetAttribute(pool, which, &bytes), "read pool counter");
  return bytes;
}
void Probe() {
  size_t available = 0, total = 0;
  Check(cudaMemGetInfo(&available, &total), "device capacity");
  if (available < 512*kMiB) throw std::runtime_error("insufficient free device memory");
  int device = -1, driver = 0, runtime = 0;
  Check(cudaGetDevice(&device), "current device");
  Check(cudaDriverGetVersion(&driver), "driver version");
  Check(cudaRuntimeGetVersion(&runtime), "runtime version");
  cudaDeviceProp hw{};
  Check(cudaGetDeviceProperties(&hw,device), "device properties");
  if (!hw.integrated || hw.major != 11 || hw.minor != 0)
    throw std::runtime_error("requires Thor SM110 integrated GPU");
  cudaMemPool_t original = nullptr, pool = nullptr, current = nullptr;
  Check(cudaDeviceGetMemPool(&original,device), "read current pool");
  cudaMemPoolProps props{};
  props.allocType = cudaMemAllocationTypePinned;
  props.location.type = cudaMemLocationTypeDevice;
  props.location.id = device;
  props.maxSize = kLimit;
  Check(cudaMemPoolCreate(&pool,&props), "create sized pool");
  if (original == pool) throw std::runtime_error("expected separate pool");
  uint64_t limit = 0, threshold = kLimit;
  const auto queried = cudaMemPoolGetAttribute(pool,cudaMemPoolAttrMaxPoolSize,&limit);
  if (queried != cudaSuccess) (void)cudaGetLastError();
  Check(cudaMemPoolSetAttribute(pool,cudaMemPoolAttrReleaseThreshold,&threshold),
        "set retention threshold");
  Check(cudaDeviceSetMemPool(device,pool), "set process pool");
  Check(cudaDeviceGetMemPool(&current,device), "verify selected pool");
  if (pool != current) throw std::runtime_error("pool did not become current");
  cudaStream_t first = nullptr, second = nullptr;
  Check(cudaStreamCreateWithFlags(&first,cudaStreamNonBlocking), "create first stream");
  Check(cudaStreamCreateWithFlags(&second,cudaStreamNonBlocking), "create second stream");
  void* live = nullptr;
  Check(cudaMallocAsync(&live,kBlock,first), "initial allocation");
  Fill(live,kBlock,0x13,first);
  const uint64_t held = Counter(pool,cudaMemPoolAttrUsedMemCurrent);
  const uint64_t backing = Counter(pool,cudaMemPoolAttrReservedMemCurrent);
  void* over = nullptr;
  const auto overflow = cudaMallocAsync(&over,kBlock,second);
  if (overflow == cudaSuccess) Fill(over,kBlock,0x75,second);
  else if (overflow != cudaErrorMemoryAllocation) Check(overflow,"second allocation");
  else (void)cudaGetLastError();
  const uint64_t both = Counter(pool,cudaMemPoolAttrUsedMemCurrent);
  const uint64_t both_reserved = Counter(pool,cudaMemPoolAttrReservedMemCurrent);
  if (overflow == cudaSuccess) {
    Check(cudaFreeAsync(over,second), "free unexpected excess");
    Check(cudaStreamSynchronize(second), "finish excess free");
  }
  // This allocation bypasses the stream-ordered pool by design.
  void* ordinary = nullptr;
  Check(cudaMalloc(&ordinary,12*kMiB), "independent device allocation");
  Fill(ordinary,12*kMiB,0x41,second);
  const uint64_t with_ordinary = Counter(pool,cudaMemPoolAttrUsedMemCurrent);
  Check(cudaFree(ordinary), "free independent allocation");
  Check(cudaFreeAsync(live,first), "free initial allocation");
  Check(cudaStreamSynchronize(first), "finish initial free");
  const uint64_t released = Counter(pool,cudaMemPoolAttrUsedMemCurrent);
  const uint64_t retained = Counter(pool,cudaMemPoolAttrReservedMemCurrent);
  void* again = nullptr;
  Check(cudaMallocAsync(&again,kBlock,second), "allocate after release");
  Fill(again,kBlock,0x28,second);
  Check(cudaFreeAsync(again,second), "release reused allocation");
  Check(cudaStreamSynchronize(second), "finish second stream");
  Check(cudaMemPoolTrimTo(pool,0), "trim pool");
  const uint64_t trimmed = Counter(pool,cudaMemPoolAttrReservedMemCurrent);
  Check(cudaDeviceSetMemPool(device,original), "restore original pool");
  Check(cudaDeviceGetMemPool(&current,device), "read restored pool");
  if (current != original) throw std::runtime_error("original pool not restored");
  Check(cudaStreamDestroy(second), "close second stream");
  Check(cudaStreamDestroy(first), "close first stream");
  Check(cudaMemPoolDestroy(pool), "close sized pool");
  std::printf("{\"driver\":%d,\"runtime\":%d,\"requested_limit\":%zu,"
              "\"attribute_status\":\"%s\",\"reported_limit\":%lu,"
              "\"allocation_size\":%zu,\"held\":%lu,\"initial_reserved\":%lu,"
              "\"concurrent_result\":\"%s\",\"both_used\":%lu,\"both_reserved\":%lu,"
              "\"ordinary_bytes\":%zu,\"pool_with_ordinary\":%lu,"
              "\"after_free\":%lu,\"retained\":%lu,\"trimmed\":%lu,"
              "\"reallocated\":true,\"original_pool_restored\":true}\n",
              driver,runtime,kLimit,cudaGetErrorName(queried),limit,kBlock,held,backing,
              cudaGetErrorName(overflow),both,both_reserved,12*kMiB,with_ordinary,
              released,retained,trimmed);
}
}  // namespace

int main() {
  try {
    Probe();
    return 0;
  } catch (const std::exception& e) {
    std::fprintf(stderr, "%s\n", e.what());
    return 1;
  }
}
