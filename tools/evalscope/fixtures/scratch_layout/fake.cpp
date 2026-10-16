#include <cuda_runtime_api.h>
#include <cassert>
#include <cerrno>
#include <cstdint>
#include <map>
static size_t allocations=0,frees=0,mallocs=0;static bool fail=false;
static std::map<void*,size_t> live;
extern "C" size_t FakeAllocations(){return allocations;}
extern "C" size_t FakeFrees(){return frees;}
extern "C" void FailNext(){fail=true;}
extern "C" cudaError_t cudaMalloc(void** p,size_t n){
 ++mallocs;*p=reinterpret_cast<void*>(mallocs==1?0x80000000ULL:0x100000100ULL);
 if(mallocs==1)assert(n==8);else assert(n>500000000 && n<600000000);
 errno=ERANGE;return cudaSuccess;
}
extern "C" cudaError_t cudaMallocAsync(void** p,size_t n,cudaStream_t s){
 assert(errno==EDOM);assert(!s);errno=ERANGE;
 if(fail){fail=false;return cudaErrorMemoryAllocation;}
 *p=reinterpret_cast<void*>(0x100000ULL+allocations*256);++allocations;live[*p]=n;return cudaSuccess;
}
extern "C" cudaError_t cudaFreeAsync(void* p,cudaStream_t s){
 assert(errno==EDOM);assert(!s);assert(live.count(p));errno=ERANGE;
 if(fail){fail=false;return cudaErrorInvalidValue;}
 live.erase(p);++frees;return cudaSuccess;
}
