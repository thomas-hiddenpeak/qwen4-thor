#include <cuda_runtime_api.h>
#include <cassert>
#include <cerrno>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <cstdint>
#include <thread>
extern "C" size_t FakeAllocations();
extern "C" size_t FakeFrees();
extern "C" void FailNext();
size_t Round(size_t n) {return (n+255)/256*256;}
size_t Bytes(int s,int t) {
  const size_t widths[]={10240,10240,6144,48,48,6144};
  return s<6?t*widths[s]*2:Round(2048)+Round(512*t*4)+Round(513*4)+Round(t*10*4);
}
int main(int argc,char** argv) {
 void* base=nullptr;errno=EDOM;assert(cudaMalloc(&base,8)==cudaSuccess);assert(errno==ERANGE);
 const bool fixed=!std::strcmp(std::getenv("Q4T_DIAG_SCRATCH_LAYOUT"),"fixed");
 if(argc>1 && !std::strcmp(argv[1],"shape")){void* p;cudaMallocAsync(&p,13,nullptr);return 2;}
 if(argc>1 && !std::strcmp(argv[1],"stream")){void* p;cudaMallocAsync(&p,Bytes(0,8192),reinterpret_cast<cudaStream_t>(1));return 2;}
 void* first[7]={};void* ptr[7]={};size_t calls=0;
 for(int chunk=0;chunk<25;++chunk)for(int layer=0;layer<48;++layer){
   int t=chunk==24?8181:8192;
   int begin=layer%4==3?6:0;
   for(int s=begin;s<7;++s){
     if(s==6 && begin==0)for(int j=0;j<6;++j){errno=EDOM;assert(cudaFreeAsync(ptr[j],nullptr)==cudaSuccess);assert(errno==ERANGE);}
     if(calls==0){FailNext();errno=EDOM;assert(cudaMallocAsync(reinterpret_cast<void**>(1),Bytes(s,t),nullptr)==cudaErrorMemoryAllocation);assert(errno==ERANGE);}
     errno=EDOM;assert(cudaMallocAsync(&ptr[s],Bytes(s,t),nullptr)==cudaSuccess);assert(errno==ERANGE);++calls;
     if(fixed){assert(reinterpret_cast<uintptr_t>(ptr[s])%(2*1024*1024)==0);if(first[s])assert(first[s]==ptr[s]);else first[s]=ptr[s];}
     if(calls==1 && argc>1 && !std::strcmp(argv[1],"foreign")){std::thread other([&]{void* p;cudaMallocAsync(&p,Bytes(1,t),nullptr);});other.join();return 2;}
     if(s==6){
       FailNext();errno=EDOM;assert(cudaFreeAsync(ptr[s],nullptr)==cudaErrorInvalidValue);assert(errno==ERANGE);
       errno=EDOM;assert(cudaFreeAsync(ptr[s],nullptr)==cudaSuccess);assert(errno==ERANGE);
     }
   }
 }
 assert(calls==6600);assert(FakeAllocations()==6600);assert(FakeFrees()==6600);
 for(int i=0;i<7;++i)for(int j=i+1;j<7;++j)if(fixed)assert(first[i]!=first[j]);
 // After the exact frozen prefix, ordinary decode allocations pass through.
 void* p=nullptr;errno=EDOM;assert(cudaMallocAsync(&p,16,nullptr)==cudaSuccess);assert(errno==ERANGE);
 assert(reinterpret_cast<uintptr_t>(p)<0x100000000ULL);errno=EDOM;assert(cudaFreeAsync(p,nullptr)==cudaSuccess);assert(errno==ERANGE);
 std::puts("CPU lifecycle and forwarding passed");
}
