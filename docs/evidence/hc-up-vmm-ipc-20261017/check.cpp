#include <cuda.h>
#include <fcntl.h>
#include <spawn.h>
#include <sys/wait.h>
#include <unistd.h>
#include <algorithm>
#include <cerrno>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <string>
#include <vector>
extern char** environ;
void Check(CUresult r, const char* operation) {
  if (r != CUDA_SUCCESS) {
    const char* name = nullptr;
    cuGetErrorName(r, &name);
    std::fprintf(stderr, "%s: %d %s\n", operation, r, name);
    std::exit(1);
  }
}
#define CHECK(expr) Check((expr), #expr)
void Posix(int error) { if (error) { std::fprintf(stderr,"POSIX error %d\n",error); std::exit(2); } }
CUdevice Init() {
  CHECK(cuInit(0));
  CUdevice device;
  CUcontext context;
  CHECK(cuDeviceGet(&device, 0));
  CHECK(cuDevicePrimaryCtxRetain(&context, device));
  CHECK(cuCtxSetCurrent(context));
  return device;
}
CUdeviceptr Map(CUmemGenericAllocationHandle handle, size_t size,
                CUdevice device, CUdeviceptr hint = 0) {
  CUdeviceptr ptr;
  CHECK(cuMemAddressReserve(&ptr, size, 0, hint, 0));
  if (hint && ptr != hint) { std::fprintf(stderr,"VA hint not honored\n"); std::exit(3); }
  CHECK(cuMemMap(ptr, size, 0, handle, 0));
  CUmemAccessDesc access{};
  access.location.type = CU_MEM_LOCATION_TYPE_DEVICE;
  access.location.id = device;
  access.flags = CU_MEM_ACCESS_FLAGS_PROT_READWRITE;
  CHECK(cuMemSetAccess(ptr, size, &access, 1));
  return ptr;
}
void Verify(CUdeviceptr ptr, size_t size, unsigned char expected) {
  std::vector<unsigned char> host(size);
  CHECK(cuCtxSynchronize());
  CHECK(cuMemcpyDtoH(host.data(), ptr, size));
  if (!std::all_of(host.begin(),host.end(),[=](auto x){return x==expected;})) {
    std::fprintf(stderr,"full-byte verification failed\n"); std::exit(4);
  }
}
void Unmap(CUdeviceptr ptr, size_t size) {
  CHECK(cuMemUnmap(ptr,size));
  CHECK(cuMemAddressFree(ptr,size));
}
int main(int argc, char** argv) {
  const CUdevice device=Init();
  if (argc==6 && std::string(argv[1])=="child") {
    const size_t size=std::strtoull(argv[2],nullptr,10);
    const int expected=std::atoi(argv[3]);
    const int next=std::atoi(argv[4]);
    const CUdeviceptr hint=std::strtoull(argv[5],nullptr,10);
    CUmemGenericAllocationHandle handle;
    CHECK(cuMemImportFromShareableHandle(&handle,
        reinterpret_cast<void*>(intptr_t{73}),CU_MEM_HANDLE_TYPE_POSIX_FILE_DESCRIPTOR));
    Posix(close(73) ? errno : 0);
    const CUdeviceptr ptr=Map(handle,size,device,hint);
    Verify(ptr,size,expected);
    CHECK(cuMemsetD8(ptr,next,size));
    CHECK(cuCtxSynchronize());
    std::printf("{\"child_pid\":%d,\"va\":%llu,\"read\":%d,\"wrote\":%d,\"verified_bytes\":%zu}\n",getpid(),ptr,expected,next,size);
    Unmap(ptr,size);
    CHECK(cuMemRelease(handle));
    CHECK(cuDevicePrimaryCtxRelease(device));
    return 0;
  }
  if (argc!=1) return 2;
  CUmemAllocationProp prop{};
  prop.type=CU_MEM_ALLOCATION_TYPE_PINNED;
  prop.location.type=CU_MEM_LOCATION_TYPE_DEVICE;
  prop.location.id=device;
  prop.requestedHandleTypes=CU_MEM_HANDLE_TYPE_POSIX_FILE_DESCRIPTOR;
  size_t granularity;
  CHECK(cuMemGetAllocationGranularity(&granularity,&prop,CU_MEM_ALLOC_GRANULARITY_MINIMUM));
  const size_t size=((2*1024*1024+granularity-1)/granularity)*granularity;
  CUmemGenericAllocationHandle handle;
  CHECK(cuMemCreate(&handle,size,&prop,0));
  int fd;
  CHECK(cuMemExportToShareableHandle(&fd,handle,CU_MEM_HANDLE_TYPE_POSIX_FILE_DESCRIPTOR,0));
  Posix(fcntl(fd,F_SETFD,FD_CLOEXEC)<0 ? errno : 0);
  const CUdeviceptr ptr=Map(handle,size,device);
  CHECK(cuMemsetD8(ptr,17,size));
  CHECK(cuCtxSynchronize());
  for (int i=0;i<2;++i) {
    const std::string bytes=std::to_string(size);
    const std::string expected=std::to_string(17*(i+1));
    const std::string next=std::to_string(17*(i+2));
    const std::string hint=std::to_string(0x4000000000ULL+i*0x20000000ULL);
    char* args[]={const_cast<char*>("/proc/self/exe"),const_cast<char*>("child"),
                  const_cast<char*>(bytes.c_str()),const_cast<char*>(expected.c_str()),
                  const_cast<char*>(next.c_str()),const_cast<char*>(hint.c_str()),nullptr};
    posix_spawn_file_actions_t actions;
    Posix(posix_spawn_file_actions_init(&actions));
    Posix(posix_spawn_file_actions_adddup2(&actions,fd,73));
    pid_t child;
    Posix(posix_spawn(&child,"/proc/self/exe",&actions,nullptr,args,environ));
    Posix(posix_spawn_file_actions_destroy(&actions));
    int status;
    pid_t waited;
    do {waited=waitpid(child,&status,0);} while(waited<0 && errno==EINTR);
    if (waited!=child || !WIFEXITED(status) || WEXITSTATUS(status)!=0) return 5;
    Verify(ptr,size,17*(i+2));
  }
  Unmap(ptr,size);
  Posix(close(fd) ? errno : 0);
  CHECK(cuMemRelease(handle));
  CHECK(cuDevicePrimaryCtxRelease(device));
  std::printf("{\"sequential_children\":2,\"shared_backing_verified\":true,\"bytes\":%zu,\"cleanup\":true}\n",size);
}
