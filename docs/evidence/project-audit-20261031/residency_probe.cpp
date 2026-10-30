// Actual residency implementation with CPU-only CUDA API doubles.
// No CUDA runtime library is linked and no GPU context is created.
#include "q4t/quant/moe_residency.h"

#include <cstdlib>
#include <cstring>
#include <iostream>
#include <memory>
#include <unordered_set>

namespace {
std::unordered_set<void*> allocations;
int malloc_calls = 0;
int fail_at = 0;
}

extern "C" {
cudaError_t cudaMalloc(void** p, size_t n) {
  if (++malloc_calls == fail_at) return cudaErrorMemoryAllocation;
  *p = std::malloc(n);
  if (!*p) return cudaErrorMemoryAllocation;
  allocations.insert(*p);
  return cudaSuccess;
}
cudaError_t cudaFree(void* p) {
  allocations.erase(p);
  std::free(p);
  return cudaSuccess;
}
cudaError_t cudaHostAlloc(void** p, size_t n, unsigned) {
  return cudaMalloc(p, n);
}
cudaError_t cudaFreeHost(void* p) { return cudaFree(p); }
cudaError_t cudaEventCreateWithFlags(cudaEvent_t* e, unsigned) {
  *e = reinterpret_cast<cudaEvent_t>(new int(0));
  return cudaSuccess;
}
cudaError_t cudaEventDestroy(cudaEvent_t e) {
  delete reinterpret_cast<int*>(e);
  return cudaSuccess;
}
cudaError_t cudaEventQuery(cudaEvent_t) { return cudaSuccess; }
cudaError_t cudaEventRecord(cudaEvent_t, cudaStream_t) { return cudaSuccess; }
cudaError_t cudaEventSynchronize(cudaEvent_t) { return cudaSuccess; }
cudaError_t cudaStreamSynchronize(cudaStream_t) { return cudaSuccess; }
cudaError_t cudaMemcpyAsync(void* d, const void* s, size_t n,
                            cudaMemcpyKind, cudaStream_t) {
  std::memcpy(d, s, n);
  return cudaSuccess;
}
const char* cudaGetErrorString(cudaError_t) { return "CPU injected failure"; }
}

int main(int argc, char** argv) {
  if (argc != 2) return 2;
  const std::string dir = argv[1];
  q4t::io::WeightIndex* raw_index = nullptr;
  if (!q4t::io::WeightIndex::Open(dir + "/index.json", &raw_index)) return 3;
  std::unique_ptr<q4t::io::WeightIndex> index(raw_index);
  q4t::io::WeightLoader* raw_loader = nullptr;
  if (!q4t::io::WeightLoader::Create(dir, *index, 2, &raw_loader)) return 4;
  std::unique_ptr<q4t::io::WeightLoader> loader(raw_loader);
  {
    fail_at = 2;
    q4t::quant::MoEResidency residency;
    const auto status = residency.Init(*loader, 0, 1, 128, 64, 1, nullptr);
    residency.Free();
    std::cout << "injected_alloc_failure status_ok=" << status.ok()
              << " allocations_after_Free=" << allocations.size() << '\n';
  }
  // Reclaim only this probe's mock host allocation after recording the leak.
  while (!allocations.empty()) cudaFree(*allocations.begin());
  fail_at = 0;
  {
    q4t::quant::MoEResidency residency;
    const auto init = residency.Init(*loader, 0, 1, 128, 64, 1, nullptr);
    if (!init) return 5;
    const auto hot = residency.InitHot({0}, nullptr);
    if (!hot) { residency.Free(); return 6; }
    const auto& layout = residency.Layout();
    std::cout << "split_shard init_ok=" << init.ok()
              << " load_ok=" << hot.ok()
              << " gate_actual=" << int(layout.gu_packed[0])
              << " gate_expected=34 up_actual="
              << int(layout.gu_packed[4096]) << " up_expected=51\n";
    residency.Free();
  }
  std::cout << "normal_Free_remaining_allocations=" << allocations.size()
            << " real_cuda_library_linked=0\n";
}
