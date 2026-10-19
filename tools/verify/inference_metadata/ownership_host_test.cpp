// CUDA fault injection without a CUDA context. Tests the actual owner class.
#include "q4t/model/decode_metadata_upload.h"
#include <cstdlib>
#include <cstring>
#include <iostream>
#include <stdexcept>
#include <vector>

namespace {
struct Copy { void* dst; const void* src; size_t bytes; };
std::vector<Copy> copies;
std::vector<void*> allocations;
int fail_copy = 0, copy_calls = 0;
bool fail_alloc = false, fail_create = false, fail_record = false;
bool fail_wait = false;
int events = 0, waits = 0;
void Require(bool value) {
  if (!value) throw std::runtime_error("ownership invariant failed");
}
void Reset() {
  Require(copies.empty() && allocations.empty() && events == 0);
  fail_copy = copy_calls = waits = 0;
  fail_alloc = fail_create = fail_record = fail_wait = false;
}
cudaError_t Wait() {
  ++waits;
  if (fail_wait) return cudaErrorUnknown;
  for (const auto& copy : copies) std::memcpy(copy.dst, copy.src, copy.bytes);
  copies.clear();
  return cudaSuccess;
}
}  // namespace
extern "C" {
cudaError_t __wrap_cudaMallocHost(void** p, size_t n) {
  if (fail_alloc) return cudaErrorMemoryAllocation;
  *p = std::malloc(n); allocations.push_back(*p); return cudaSuccess;
}
cudaError_t __wrap_cudaHostAlloc(void** p, size_t n, unsigned) {
  return __wrap_cudaMallocHost(p, n);
}
cudaError_t __wrap_cudaFreeHost(void* p) {
  for (auto it = allocations.begin(); it != allocations.end(); ++it) {
    if (*it == p) { allocations.erase(it); std::free(p); return cudaSuccess; }
  }
  std::abort();
}
cudaError_t __wrap_cudaEventCreateWithFlags(cudaEvent_t* e, unsigned) {
  if (fail_create) return cudaErrorMemoryAllocation;
  *e = reinterpret_cast<cudaEvent_t>(new int(0)); ++events; return cudaSuccess;
}
cudaError_t __wrap_cudaEventDestroy(cudaEvent_t e) {
  delete reinterpret_cast<int*>(e); --events; return cudaSuccess;
}
cudaError_t __wrap_cudaMemcpyAsync(void* d, const void* s, size_t n,
                                  cudaMemcpyKind, cudaStream_t) {
  if (++copy_calls == fail_copy) return cudaErrorInvalidValue;
  copies.push_back({d, s, n}); return cudaSuccess;
}
cudaError_t __wrap_cudaEventRecord(cudaEvent_t, cudaStream_t) {
  return fail_record ? cudaErrorInvalidValue : cudaSuccess;
}
cudaError_t __wrap_cudaEventSynchronize(cudaEvent_t) { return Wait(); }
cudaError_t __wrap_cudaStreamSynchronize(cudaStream_t) { return Wait(); }
const char* __wrap_cudaGetErrorString(cudaError_t) { return "injected error"; }
}
int main() {
  using q4t::model::DecodeMetadataUpload;
  try {
    int ids[2] = {17, 23}, pos[2] = {1, 2}, slots[2] = {1, 0};
    int delta[2] = {3, 4}, di[2]{}, dp[2]{}, ds[2]{}, rope[48]{};
    DecodeMetadataUpload::Targets targets{di, dp, ds, rope, 8, 2};
    int cases = 0;
    for (int failure = 0; failure < 7; ++failure) {
      Reset();
      {
        DecodeMetadataUpload upload;
        fail_alloc = failure == 0; fail_create = failure == 1;
        if (failure < 2) {
          Require(!upload.Init(2).ok());
          Require(allocations.empty() && events == 0);
        } else {
          Require(upload.Init(2).ok());
          fail_copy = failure == 2 ? 1 : failure == 3 ? 4 : failure == 4 ? 9 : 0;
          fail_record = failure == 5;
          fail_wait = failure == 6;
          const auto submitted = upload.Upload(ids, pos, slots, delta, targets, nullptr);
          if (failure == 6) {
            Require(submitted.ok());
            Require(!upload.Drain().ok());
            const int calls = copy_calls;
            Require(!upload.Upload(ids, pos, slots, delta, targets, nullptr).ok());
            Require(copy_calls == calls && !allocations.empty());
            // Recovery can drain old data but the failed owner stays poisoned.
            fail_wait = false;
            Require(!upload.Drain().ok() && copies.empty());
          } else {
            Require(!submitted.ok() && copies.empty() && waits == 1);
            const int calls = copy_calls;
            Require(!upload.Upload(ids, pos, slots, delta, targets, nullptr).ok());
            Require(copy_calls == calls);
          }
        }
      }
      Require(allocations.empty() && copies.empty() && events == 0);
      ++cases;
    }
    Reset();
    {
      DecodeMetadataUpload upload;
      Require(upload.Init(2).ok());
      Require(upload.Upload(ids, pos, slots, delta, targets, nullptr).ok());
      ids[0] = 99; pos[0] = 7; slots[0] = 0; delta[1] = 99;
      // Destructor must drain its own immutable source, not caller memory.
    }
    Require(di[0] == 17 && dp[0] == 1 && ds[0] == 1 && rope[25] == 5);
    Require(allocations.empty() && copies.empty() && events == 0);
    std::cout << "PASS " << cases + 1 << " host ownership/fault cases\n";
  } catch (const std::exception& e) {
    std::cerr << "FAIL " << e.what() << '\n'; return 1;
  }
}
