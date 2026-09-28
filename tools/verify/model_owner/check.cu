#include "q4t/model/model_owner.h"

#include <cstdio>
#include <cstdlib>
#include <type_traits>
#include <unordered_set>

using q4t::model::ModelOwner;
static_assert(!std::is_copy_constructible_v<ModelOwner>);
static_assert(!std::is_move_constructible_v<ModelOwner>);
namespace {
std::unordered_set<void*> live;
int allocations = 0, frees = 0, fail_at = 0, syncs = 0;
bool require_drain = false;
void Check(bool ok, const char* message) {
  if (!ok) { std::fprintf(stderr, "%s\n", message); std::exit(1); }
}
}  // namespace
extern "C" cudaError_t __real_cudaMalloc(void**, size_t);
extern "C" cudaError_t __real_cudaFree(void*);
extern "C" cudaError_t __real_cudaStreamSynchronize(cudaStream_t);
extern "C" cudaError_t __wrap_cudaMalloc(void** p, size_t n) {
  ++allocations;
  if (fail_at && allocations == fail_at) return cudaErrorMemoryAllocation;
  const auto result = __real_cudaMalloc(p, n);
  if (result == cudaSuccess) Check(live.insert(*p).second, "duplicate live allocation");
  return result;
}
extern "C" cudaError_t __wrap_cudaFree(void* p) {
  if (p) {
    if (require_drain) Check(syncs > 0, "freed partial load before drain");
    Check(live.erase(p) == 1, "free without ownership or double free");
    ++frees;
  }
  const auto result = __real_cudaFree(p);
  Check(result == cudaSuccess, "cudaFree failed");
  return result;
}
extern "C" cudaError_t __wrap_cudaStreamSynchronize(cudaStream_t stream) {
  ++syncs;
  return __real_cudaStreamSynchronize(stream);
}
int main(int argc, char** argv) {
  Check(argc == 3, "requires model directory and empty test index");
  Check(cudaSetDevice(0) == cudaSuccess, "CUDA required; no SKIP");
  { ModelOwner empty; }
  Check(live.empty() && frees == 0, "empty destruction");
  q4t::model::ModelConfig tiny;
  tiny.model_dir = ".";
  tiny.index_path = argv[2];
  tiny.hs = tiny.vocab = 1;
  require_drain = true;
  {
    ModelOwner owner;
    for (int attempt = 0; attempt < 2; ++attempt) {
      allocations = frees = syncs = 0;
      const auto status = owner.Load(tiny, nullptr);
      Check(!status.ok(), "missing tensor must fail");
      Check(allocations == 2 && frees == 2 && live.empty(), "partial load leaked");
      Check(owner.Get().head.embed_tokens == nullptr &&
            owner.Get().head.lm_head == nullptr, "dangling head after failure");
    }
    allocations = frees = syncs = 0; fail_at = 2;
    Check(!owner.Load(tiny, nullptr).ok(), "allocation failure not propagated");
    Check(live.empty() && frees == 1, "allocation failure cleanup");
    fail_at = 0;
  }
  require_drain = false;
  q4t::model::ModelConfig cfg;
  cfg.model_dir = argv[1];
  cfg.index_path = cfg.model_dir + "/model.safetensors.index.json";
  cfg.ple_sidecar = cfg.model_dir + "/ple/qwen3.8-flash-next-ple-fp8.bin";
  cfg.max_len = 64; cfg.max_prefill = 32; cfg.ple_capacity_tokens = 32;
  for (int boundary : {40, 1500}) {
    allocations = frees = syncs = 0; fail_at = boundary;
    {
      ModelOwner owner;
      Check(!owner.Load(cfg, nullptr).ok(), "partial full-model load must fail");
      Check(syncs > 0 && live.empty(), "partial layer/model load leaked");
    }
    Check(live.empty(), "partial model destruction leaked");
    std::printf("partial model failure %d: %d allocations, %d frees\n",
                boundary, allocations, frees);
    fail_at = 0;
  }
  for (int repeat = 0; repeat < 2; ++repeat) {
    allocations = frees = syncs = 0;
    {
      ModelOwner owner;
      const auto status = owner.Load(cfg, nullptr);
      if (!status.ok()) std::fprintf(stderr, "%s\n", status.message().c_str());
      Check(status.ok() && !live.empty(), "full load failed");
      const int before = allocations;
      const auto* embedding = owner.Get().head.embed_tokens;
      Check(!owner.Load(cfg, nullptr).ok(), "live reload must reject");
      Check(before == allocations && embedding == owner.Get().head.embed_tokens,
            "live reload invalidated borrowed weights");
    }
    Check(live.empty() && frees == allocations, "full model destruction leaked");
    std::printf("full model cycle %d: %d allocations freed exactly once\n",
                repeat, frees);
  }
  std::puts("owner: empty, partial failure, retry, allocation failure, live reload, full destruction: PASS");
}
