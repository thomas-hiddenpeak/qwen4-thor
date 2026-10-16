#include <cuda.h>
#include <cuda_runtime_api.h>
#include <algorithm>
#include <cassert>
#include <cstdio>
#include <cstdlib>
#include <vector>
extern "C" void VerifyLegacy();
void Check(CUresult r) {
 if(r!=CUDA_SUCCESS) {std::fprintf(stderr,"CUDA failure %d\n",r);std::exit(1);}
}
void Verify(CUdeviceptr ptr,size_t bytes,unsigned char expected) {
 std::vector<unsigned char> host(8*1024*1024);
 for(size_t offset=0;offset<bytes;offset+=host.size()) {
  size_t n=std::min(host.size(),bytes-offset);
  Check(cuMemcpyDtoH(host.data(),ptr+offset,n));
  assert(std::all_of(host.begin(),host.begin()+n,[=](auto b){return b==expected;}));
 }
}
int main(int argc,char** argv) {
 assert(argc==3);int step=std::atoi(argv[2]);assert(step==0 || step==1);
 Check(cuInit(0));CUdevice device;CUcontext ctx;
 Check(cuDeviceGet(&device,0));Check(cuDevicePrimaryCtxRetain(&ctx,device));
 Check(cuCtxSetCurrent(ctx));
 FILE* f=std::fopen(argv[1],"r");assert(f);
 void* pointers[1541];size_t sizes[1541];
 for(size_t i=0;i<1541;++i) {
  assert(std::fscanf(f,"%zu",&sizes[i])==1);
  assert(cudaMalloc(&pointers[i],sizes[i])==cudaSuccess);
 }
 assert(std::fclose(f)==0);
 int indices[]={1534,1535,1537};
 for(int i=0;i<3;++i) {
  CUdeviceptr p=reinterpret_cast<CUdeviceptr>(pointers[indices[i]]);
  const CUdeviceptr expected=0x4000000000ULL+i*0x100000000ULL+
      (step ? 536870912 : 0);
  assert(p==expected);
  size_t n=sizes[indices[i]];
  if(step) Verify(p,n,17+i);
  Check(cuMemsetD8(p,(step+1)*17+i,n));Check(cuCtxSynchronize());
  Verify(p,n,(step+1)*17+i);
  std::printf("{\"step\":%d,\"slot\":%d,\"va\":%llu,\"verified_bytes\":%zu,\"cross_process_verified\":%s}\n",step,i,p,n,step?"true":"false");
 }
 for(int i=1540;i>=0;--i) assert(cudaFree(pointers[i])==cudaSuccess);
 VerifyLegacy();Check(cuDevicePrimaryCtxRelease(device));
}
