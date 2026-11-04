// Real CUDA allocations/uploads; fixed failure sweep, no model generation.
#include "q4t/io/weight_loader.h"
#include "q4t/vision/vision.h"

#include <array>
#include <cstdio>
#include <cstdlib>
#include <memory>
#include <new>
#include <string>
#include <vector>

namespace {
std::array<void*, 1024> live{};
int allocations = 0, fail_at = 0, copies = 0, copy_fail = 0;
int async_copies = 0, host_after_copy = 0, syncs = 0;
bool host_fail = false;

void Check(bool condition, const char* message) {
  if (!condition) {
    std::fprintf(stderr, "FAIL: %s\n", message);
    std::exit(1);
  }
}
size_t Live() {
  size_t count = 0;
  for (auto* p : live) count += p != nullptr;
  return count;
}
void Reset(int failure = 0) {
  allocations = copies = async_copies = syncs = 0;
  fail_at = failure;
  copy_fail = host_after_copy = 0;
  host_fail = false;
}
void EmptyWorkspace(const q4t::vision::VisionTower& t) {
  Check(!t.d_ws && !t.d_cos && !t.d_sin && !t.d_cos_w && !t.d_sin_w &&
            !t.d_pos_rows && !t.d_pos_weights && !t.d_pos_ids &&
            t.ws_bytes == 0 && t.max_grid == 0,
        "stale workspace");
}
struct Loader {
  std::unique_ptr<q4t::io::WeightIndex> index;
  std::unique_ptr<q4t::io::WeightLoader> loader;
  explicit Loader(const std::string& directory) {
    q4t::io::WeightIndex* i = nullptr;
    Check(q4t::io::WeightIndex::Open(
              directory + "/model.safetensors.index.json", &i)
              .ok(),
          "index");
    index.reset(i);
    q4t::io::WeightLoader* l = nullptr;
    Check(q4t::io::WeightLoader::Create(directory, *i, 8, &l).ok(), "loader");
    loader.reset(l);
  }
};
}  // namespace

extern "C" cudaError_t __real_cudaMalloc(void**, size_t);
extern "C" cudaError_t __real_cudaFree(void*);
extern "C" cudaError_t __real_cudaMemcpy(void*, const void*, size_t,
                                         cudaMemcpyKind);
extern "C" cudaError_t __real_cudaMemcpyAsync(void*, const void*, size_t,
                                              cudaMemcpyKind, cudaStream_t);
extern "C" cudaError_t __real_cudaStreamSynchronize(cudaStream_t);
extern "C" void* __real__Znwm(size_t);
extern "C" void* __wrap__Znwm(size_t bytes) {
  if (host_fail) {
    host_fail = false;
    throw std::bad_alloc();
  }
  return __real__Znwm(bytes);
}
extern "C" cudaError_t __wrap_cudaMalloc(void** p, size_t n) {
  if (++allocations == fail_at) return cudaErrorMemoryAllocation;
  const auto result = __real_cudaMalloc(p, n);
  if (result == cudaSuccess) {
    bool recorded = false;
    for (auto*& slot : live) {
      if (!slot) {
        slot = *p;
        recorded = true;
        break;
      }
    }
    Check(recorded, "live capacity");
  }
  return result;
}
extern "C" cudaError_t __wrap_cudaFree(void* p) {
  if (p) {
    bool found = false;
    for (auto*& slot : live) {
      if (slot == p) {
        slot = nullptr;
        found = true;
        break;
      }
    }
    Check(found, "double free or foreign pointer");
  }
  const auto result = __real_cudaFree(p);
  Check(result == cudaSuccess, "real free failed");
  return result;
}
extern "C" cudaError_t __wrap_cudaMemcpy(void* d, const void* s, size_t n,
                                         cudaMemcpyKind k) {
  if (++copies == copy_fail) return cudaErrorInvalidValue;
  return __real_cudaMemcpy(d, s, n, k);
}
extern "C" cudaError_t __wrap_cudaMemcpyAsync(void* d, const void* s, size_t n,
                                              cudaMemcpyKind k,
                                              cudaStream_t st) {
  const auto result = __real_cudaMemcpyAsync(d, s, n, k, st);
  if (++async_copies == host_after_copy) host_fail = true;
  return result;
}
extern "C" cudaError_t __wrap_cudaStreamSynchronize(cudaStream_t st) {
  ++syncs;
  return __real_cudaStreamSynchronize(st);
}

int main(int argc, char** argv) {
  Check(argc == 3, "vision_contract tiny_directory missing_directory");
  q4t::vision::VisionConfig cfg;
  cfg.depth = 2;
  cfg.hidden_size = 8;
  cfg.intermediate_size = 16;
  cfg.num_heads = 1;
  cfg.head_dim = 8;
  cfg.patch_size = 2;
  cfg.out_hidden_size = 8;
  cfg.num_position_embeddings = 16;
  Loader full(argv[1]), missing(argv[2]);
  std::string error;
  for (int failure = 1; failure <= 33; ++failure) {
    q4t::vision::VisionTower tower;
    Reset(failure);
    Check(!q4t::vision::LoadVision(*full.loader, cfg, &tower, &error),
          "allocation failure unexpectedly loaded");
    Check(allocations == failure && syncs > 0 && tower.Empty() && Live() == 0,
          "partial weights were not drained/released");
    tower.Free();
  }
  for (int after : {1, 4, 15}) {
    q4t::vision::VisionTower tower;
    Reset();
    host_after_copy = after;
    bool caught = false;
    try {
      q4t::vision::LoadVision(*full.loader, cfg, &tower, &error);
    } catch (const std::bad_alloc&) {
      caught = true;
    }
    Check(caught && syncs > 0 && tower.Empty() && Live() == 0,
          "host exception leaked weight ownership");
  }
  {
    q4t::vision::VisionTower tower;
    Reset();
    Check(!q4t::vision::LoadVision(*missing.loader, cfg, &tower, &error),
          "missing tensor unexpectedly loaded");
    Check(allocations == 27 && syncs > 0 && tower.Empty() && Live() == 0,
          "partially loaded block leaked");
  }
  {
    q4t::vision::VisionTower tower;
    Reset();
    Check(q4t::vision::LoadVision(*full.loader, cfg, &tower, &error),
          "tiny load");
    const auto weights = Live();
    Check(weights == 33 && syncs > 0, "upload completion/weights");
    Check(!q4t::vision::LoadVision(*full.loader, cfg, &tower, &error) &&
              Live() == weights,
          "live reload destroyed ownership");
    const std::vector<q4t::vision::ImageShape> small{{2, 2, 1}},
        large{{4, 4, 1}};
    int failures = 0;
    for (int mode = 0; mode < 3; ++mode) {
      for (int failure = 1; failure <= (mode == 1 ? 4 : 8); ++failure) {
        Reset();
        if (mode) Check(tower.Allocate(small, nullptr), "reuse setup");
        Reset(failure);
        q4t::runtime::DeviceMemoryResult result;
        Check(!tower.Allocate(mode == 2 ? large : small, nullptr, &result) &&
                  result.exhausted(),
              "workspace failure classification");
        Check(allocations == failure && syncs > 0 && Live() == weights,
              "workspace failure leaked or released weights");
        EmptyWorkspace(tower);
        Reset();
        Check(tower.Allocate(small, nullptr), "OOM retry failed");
        Check(Live() == weights + 8, "retry ownership");
        tower.FreeWorkspace();
        EmptyWorkspace(tower);
        ++failures;
      }
    }
    for (int failure = 1; failure <= 4; ++failure) {
      Reset();
      copy_fail = failure;
      q4t::runtime::DeviceMemoryResult result;
      Check(!tower.Allocate(small, nullptr, &result) &&
                result.state == q4t::runtime::DeviceMemoryState::kFailure,
            "table copy incorrectly recoverable");
      Check(syncs > 0 && Live() == weights, "table copy cleanup");
      EmptyWorkspace(tower);
    }
    std::printf("PASS vision_workspace oom=%d copy_failures=4\n", failures);
    // Destructor owns all 33 weights. No explicit Free here.
  }
  Check(Live() == 0, "tower destructor leaked");
  std::printf("PASS vision_weights oom=33 host=3 missing=1 success=1\n");
}
