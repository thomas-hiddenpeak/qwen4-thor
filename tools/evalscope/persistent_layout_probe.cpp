// Diagnostic only: frozen 8192/208896, max_seq=1 OFF fixture.
// Requires an exact startup cudaMalloc size manifest and three inherited VMM
// FDs (73..75). Both arms forward and retain every legacy allocation, import
// the same handles, and map the same VAs. Only the returned pointers differ.
// Never use this as a production allocator or evidence of a pure VA effect.
#include <cuda.h>
#include <cuda_runtime_api.h>
#include <dlfcn.h>
#include <unistd.h>

#include <atomic>
#include <cerrno>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>

namespace {
constexpr size_t kCount = 1541;
constexpr size_t kIndices[] = {1534, 1535, 1537};
constexpr size_t kSizes[] = {167772160, 167772160, 2348023808};
constexpr size_t kGranularity = 2097152;
size_t expected[kCount];
size_t cursor = 0;
bool initialized = false;
bool mapped = false;
bool shared = false;
unsigned freed = 0;
CUdeviceptr shift = 0;
std::atomic_flag busy = ATOMIC_FLAG_INIT;
struct Slot {
  void* original = nullptr;
  void* exposed = nullptr;
  CUdeviceptr va = 0;
  CUmemGenericAllocationHandle handle = 0;
  size_t bytes = 0;
};
Slot slots[3];
[[noreturn]] void Die(const char* message) {
  std::fprintf(stderr, "[persistent-layout] INVALID %s\n", message);
  std::abort();
}
struct Entry {
  Entry() {
    if (busy.test_and_set()) Die("concurrent or recursive allocation/free");
  }
  ~Entry() { busy.clear(); }
};
template <class Fn>
Fn Next(const char* name) {
  auto fn = reinterpret_cast<Fn>(dlsym(RTLD_NEXT, name));
  if (!fn) Die("missing runtime symbol");
  return fn;
}
void Check(CUresult result) {
  if (result != CUDA_SUCCESS) {
    std::fprintf(stderr, "[persistent-layout] driver_error=%d\n", result);
    Die("driver operation failed");
  }
}
void Initialize() {
  const char* mode = std::getenv("Q4T_DIAG_PERSISTENT_MODE");
  if (!mode || (std::strcmp(mode, "original") && std::strcmp(mode, "shared")))
    Die("explicit original/shared mode required");
  shared = !std::strcmp(mode, "shared");
  const char* offset = std::getenv("Q4T_DIAG_PERSISTENT_SHIFT");
  if (!offset || (std::strcmp(offset, "0") && std::strcmp(offset, "512")))
    Die("explicit shift 0/512 MiB required");
  shift = !std::strcmp(offset, "512") ? 536870912 : 0;
  const char* path = std::getenv("Q4T_DIAG_PERSISTENT_MANIFEST");
  if (!path) Die("manifest required");
  FILE* file = std::fopen(path, "r");
  if (!file) Die("manifest open failed");
  for (size_t& n : expected) {
    if (std::fscanf(file, "%zu", &n) != 1 || n == 0)
      Die("invalid manifest entry");
  }
  char extra;
  if (std::fscanf(file, " %c", &extra) != EOF || std::ferror(file))
    Die("manifest has trailing data or read error");
  if (std::fclose(file)) Die("manifest close failed");
  for (size_t i = 0; i < 3; ++i) {
    if (expected[kIndices[i]] != kSizes[i]) Die("wrong target manifest");
  }
  initialized = true;
}
void Map() {
  CUdevice device;
  Check(cuCtxGetDevice(&device));
  if (device != 0) Die("unexpected device");
  CUmemAllocationProp prop{};
  prop.type = CU_MEM_ALLOCATION_TYPE_PINNED;
  prop.location.type = CU_MEM_LOCATION_TYPE_DEVICE;
  prop.location.id = device;
  prop.requestedHandleTypes = CU_MEM_HANDLE_TYPE_POSIX_FILE_DESCRIPTOR;
  size_t granularity;
  Check(cuMemGetAllocationGranularity(&granularity, &prop,
                                    CU_MEM_ALLOC_GRANULARITY_MINIMUM));
  if (granularity != kGranularity) Die("granularity changed");
  for (size_t i = 0; i < 3; ++i) {
    Slot& slot = slots[i];
    slot.bytes = (kSizes[i] + granularity - 1) / granularity * granularity;
    const int fd = 73 + i;
    Check(cuMemImportFromShareableHandle(
        &slot.handle, reinterpret_cast<void*>(static_cast<intptr_t>(fd)),
        CU_MEM_HANDLE_TYPE_POSIX_FILE_DESCRIPTOR));
    if (close(fd)) Die("imported fd close failed");
    const CUdeviceptr hint = 0x4000000000ULL + i * 0x100000000ULL + shift;
    Check(cuMemAddressReserve(&slot.va, slot.bytes, 0, hint, 0));
    if (slot.va != hint) Die("exact VA unavailable");
    Check(cuMemMap(slot.va, slot.bytes, 0, slot.handle, 0));
    CUmemAccessDesc access{};
    access.location = prop.location;
    access.flags = CU_MEM_ACCESS_FLAGS_PROT_READWRITE;
    Check(cuMemSetAccess(slot.va, slot.bytes, &access, 1));
  }
  mapped = true;
}
}  // namespace

extern "C" cudaError_t cudaMalloc(void** pointer, size_t bytes) {
  const int incoming = errno;
  Entry entry;
  static auto real = Next<decltype(&cudaMalloc)>("cudaMalloc");
  if (!initialized) Initialize();
  if (cursor < kCount && expected[cursor] != bytes)
    Die("startup allocation sequence mismatch");
  errno = incoming;
  const cudaError_t result = real(pointer, bytes);
  const int saved = errno;
  if (result != cudaSuccess) Die("legacy allocation failed");
  for (size_t i = 0; i < 3; ++i) {
    if (cursor != kIndices[i]) continue;
    if (!pointer || !*pointer) Die("successful allocation has no pointer");
    if (!mapped) Map();
    Slot& slot = slots[i];
    slot.original = *pointer;
    slot.exposed = shared ? reinterpret_cast<void*>(slot.va) : *pointer;
    *pointer = slot.exposed;
    std::fprintf(stderr,
                 "[persistent-layout] slot=%zu original=%p va=%llu "
                 "exposed=%p bytes=%zu mode=%s\n",
                 i, slot.original, slot.va, slot.exposed, bytes,
                 shared ? "shared" : "original");
  }
  ++cursor;
  if (cursor == kCount)
    std::fprintf(stderr, "[persistent-layout] startup_complete=%zu\n", cursor);
  errno = saved;
  return result;
}

extern "C" cudaError_t cudaFree(void* pointer) {
  const int incoming = errno;
  Entry entry;
  static auto real = Next<decltype(&cudaFree)>("cudaFree");
  for (auto& slot : slots) {
    if (!slot.original || pointer != slot.exposed) continue;
    // cudaFree of the unused legacy allocation does not synchronize accesses
    // to the VMM alias. Explicitly finish device work before releasing either.
    Check(cuCtxSynchronize());
    errno = incoming;
    const cudaError_t result = real(slot.original);
    const int saved = errno;
    if (result != cudaSuccess) Die("legacy free failed");
    Check(cuMemUnmap(slot.va, slot.bytes));
    Check(cuMemAddressFree(slot.va, slot.bytes));
    Check(cuMemRelease(slot.handle));
    slot.original = slot.exposed = nullptr;
    ++freed;
    if (freed == 3) {
      if (cursor < kCount) Die("freed before startup completion");
      std::fprintf(stderr, "[persistent-layout] cleanup_complete=3\n");
    }
    errno = saved;
    return result;
  }
  for (const auto& slot : slots) {
    const auto p = reinterpret_cast<uintptr_t>(pointer);
    if (slot.va && p >= slot.va && p < slot.va + slot.bytes)
      Die("free of inactive or interior VMM pointer");
  }
  errno = incoming;
  return real(pointer);
}
