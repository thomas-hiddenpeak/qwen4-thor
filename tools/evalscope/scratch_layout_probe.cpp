// OFF-only diagnostic for the frozen 204789-token fixture, not production.
// Both modes reserve the same arena and forward every original allocation.
// "fixed" substitutes seven disjoint scratch slots and translates their frees;
// "control" returns original addresses. The arena lives until process exit.
// This changes used placement (including physical pages), not only VA bits.
#include <cuda_runtime_api.h>
#include <dlfcn.h>
#include <sys/syscall.h>
#include <unistd.h>

#include <atomic>
#include <cerrno>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>

namespace {
constexpr size_t kAlign = 2 * 1024 * 1024;
constexpr size_t kCalls = 25 * 264;
constexpr size_t kWidths[] = {10240, 10240, 6144, 48, 48, 6144};
size_t Round(size_t n, size_t a) { return (n + a - 1) / a * a; }
size_t Bytes(size_t slot, size_t t) {
  if (slot < 6) return t * kWidths[slot] * 2;
  return Round(512 * 4, 256) + Round(512 * t * 4, 256) + Round(513 * 4, 256) +
         Round(t * 10 * 4, 256);
}
[[noreturn]] void Die(const char* message) {
  std::fprintf(stderr, "[scratch-layout] INVALID %s\n", message);
  std::abort();
}
template <class Fn>
Fn Next(const char* name) {
  auto fn = reinterpret_cast<Fn>(dlsym(RTLD_NEXT, name));
  if (!fn) Die("missing CUDA symbol");
  return fn;
}
bool Fixed() {
  static bool fixed = [] {
    const char* v = std::getenv("Q4T_DIAG_SCRATCH_LAYOUT");
    if (!v || (std::strcmp(v, "control") && std::strcmp(v, "fixed")))
      Die("explicit control/fixed mode required");
    return std::strcmp(v, "fixed") == 0;
  }();
  return fixed;
}
struct Slot {
  void* original = nullptr;
  void* exposed = nullptr;
  size_t offset = 0;
};
Slot slots[7];
void* arena = nullptr;
size_t arena_bytes = 0;
uintptr_t aligned_base = 0;
std::atomic<size_t> cursor{0};
std::atomic<size_t> frees{0};
std::atomic<bool> initialized{false};
std::atomic<long> owner{0};
void Owner() {
  const long tid = syscall(SYS_gettid);
  long empty = 0;
  owner.compare_exchange_strong(empty, tid);
  if (owner.load() != tid) Die("concurrent/foreign scratch thread");
}
}  // namespace

extern "C" cudaError_t cudaMalloc(void** p, size_t n) {
  const int incoming = errno;
  static auto real = Next<decltype(&cudaMalloc)>("cudaMalloc");
  const bool fixed = Fixed();
  errno = incoming;
  const cudaError_t rc = real(p, n);
  const int saved = errno;
  if (rc == cudaSuccess && !initialized.exchange(true)) {
    size_t total = 0;
    for (size_t i = 0; i < 7; ++i) {
      slots[i].offset = total;
      total += Round(Bytes(i, 8192), kAlign);
    }
    arena_bytes = total + kAlign;
    if (real(&arena, arena_bytes) != cudaSuccess)
      Die("arena allocation failed");
    aligned_base = Round(reinterpret_cast<uintptr_t>(arena), kAlign);
    std::fprintf(stderr,
                 "[scratch-layout] mode=%s arena=%p aligned=%p bytes=%zu\n",
                 fixed ? "fixed" : "control", arena,
                 reinterpret_cast<void*>(aligned_base), arena_bytes);
  }
  errno = saved;
  return rc;
}

extern "C" cudaError_t cudaMallocAsync(void** p, size_t n,
                                       cudaStream_t stream) {
  const int incoming = errno;
  static auto real = Next<decltype(&cudaMallocAsync)>("cudaMallocAsync");
  const bool fixed = Fixed();
  if (cursor >= kCalls) {
    errno = incoming;
    return real(p, n, stream);
  }
  Owner();
  if (!arena || stream != nullptr) Die("arena missing or nonzero stream");
  const size_t local = cursor % 22;
  const size_t slot = local == 21 ? 6 : local % 7;
  const size_t t = cursor < 24 * 264 ? 8192 : 8181;
  if (n != Bytes(slot, t) || slots[slot].original)
    Die("unexpected allocation shape/order or live slot reuse");
  errno = incoming;
  const cudaError_t rc = real(p, n, stream);
  const int saved = errno;
  if (rc == cudaSuccess) {
    if (!p || !*p) Die("successful allocation has no output");
    slots[slot].original = *p;
    slots[slot].exposed =
        fixed ? reinterpret_cast<void*>(aligned_base + slots[slot].offset) : *p;
    *p = slots[slot].exposed;
    ++cursor;
  }
  errno = saved;
  return rc;
}

extern "C" cudaError_t cudaFreeAsync(void* p, cudaStream_t stream) {
  const int incoming = errno;
  static auto real = Next<decltype(&cudaFreeAsync)>("cudaFreeAsync");
  if (frees < kCalls) Owner();
  for (auto& slot : slots) {
    if (!slot.original || p != slot.exposed) continue;
    Owner();
    if (stream != nullptr) Die("scratch free on another stream");
    errno = incoming;
    const cudaError_t rc = real(slot.original, stream);
    const int saved = errno;
    if (rc == cudaSuccess) {
      slot.original = slot.exposed = nullptr;
      ++frees;
      if (frees == kCalls) {
        if (cursor != kCalls) Die("invalid completion count");
        std::fprintf(stderr,
                     "[scratch-layout] complete allocations=%zu frees=%zu\n",
                     cursor.load(), frees.load());
      }
    }
    errno = saved;
    return rc;
  }
  const uintptr_t value = reinterpret_cast<uintptr_t>(p);
  if (arena && value >= reinterpret_cast<uintptr_t>(arena) &&
      value < reinterpret_cast<uintptr_t>(arena) + arena_bytes)
    Die("free of inactive arena slot");
  errno = incoming;
  return real(p, stream);
}
