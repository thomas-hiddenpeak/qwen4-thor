// Fake legacy runtime allocations only; VMM operations use the real driver.
#include <cuda_runtime_api.h>
#include <cassert>
#include <cstdint>
static uintptr_t count=0, freed=0;
extern "C" cudaError_t cudaMalloc(void** p,size_t) {
 *p=reinterpret_cast<void*>(0x10000+4096*count++);return cudaSuccess;
}
extern "C" cudaError_t cudaFree(void* p) {
 auto n=reinterpret_cast<uintptr_t>(p);
 assert(n>=0x10000 && n<0x10000+1541*4096 && n%4096==0);
 ++freed;return cudaSuccess;
}
extern "C" void VerifyLegacy() {assert(count==1541 && freed==1541);}
