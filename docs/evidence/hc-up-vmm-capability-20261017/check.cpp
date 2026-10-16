#include <cuda.h>
#include <cuda_runtime_api.h>
#include <algorithm>
#include <cstdio>
#include <cstdlib>
#include <vector>
void Check(CUresult result, const char* operation) {
  if (result != CUDA_SUCCESS) {
    const char* name = nullptr;
    cuGetErrorName(result, &name);
    std::fprintf(stderr, "%s: %d %s\n", operation, result, name);
    std::exit(1);
  }
}
#define CHECK(expr) Check((expr), #expr)
int main() {
  CHECK(cuInit(0));
  CUdevice device;
  CHECK(cuDeviceGet(&device, 0));
  int supported = 0;
  CHECK(cuDeviceGetAttribute(&supported,
      CU_DEVICE_ATTRIBUTE_VIRTUAL_MEMORY_MANAGEMENT_SUPPORTED, device));
  std::printf("{\"vmm_supported\":%d}\n", supported);
  if (!supported) return 2;
  CUcontext context;
  CHECK(cuDevicePrimaryCtxRetain(&context, device));
  CHECK(cuCtxSetCurrent(context));
  void* legacy = nullptr;
  if (cudaMalloc(&legacy, 2 * 1024 * 1024) != cudaSuccess) return 1;
  CUmemGenericAllocationHandle retained{};
  const CUresult legacy_result = cuMemRetainAllocationHandle(&retained, legacy);
  std::printf("{\"legacy_cudaMalloc_retain_result\":%d}\n", legacy_result);
  if (legacy_result == CUDA_SUCCESS) CHECK(cuMemRelease(retained));
  if (cudaFree(legacy) != cudaSuccess) return 1;
  CUmemAllocationProp prop{};
  prop.type = CU_MEM_ALLOCATION_TYPE_PINNED;
  prop.location.type = CU_MEM_LOCATION_TYPE_DEVICE;
  prop.location.id = device;
  size_t granularity = 0;
  CHECK(cuMemGetAllocationGranularity(&granularity, &prop,
                                    CU_MEM_ALLOC_GRANULARITY_MINIMUM));
  const size_t size = ((2 * 1024 * 1024 + granularity - 1) / granularity) * granularity;
  CUmemGenericAllocationHandle handle;
  CHECK(cuMemCreate(&handle, size, &prop, 0));
  CUdeviceptr a, b;
  CHECK(cuMemAddressReserve(&a, size, granularity, 0, 0));
  CHECK(cuMemAddressReserve(&b, size, granularity, 0, 0));
  if (a == b) return 3;
  CHECK(cuMemMap(a, size, 0, handle, 0));
  CHECK(cuMemMap(b, size, 0, handle, 0));
  CUmemAccessDesc access{};
  access.location = prop.location;
  access.flags = CU_MEM_ACCESS_FLAGS_PROT_READWRITE;
  CHECK(cuMemSetAccess(a, size, &access, 1));
  CHECK(cuMemSetAccess(b, size, &access, 1));
  CHECK(cuMemRetainAllocationHandle(&retained, reinterpret_cast<void*>(a)));
  const bool same_handle = retained == handle;
  CHECK(cuMemRelease(retained));
  std::vector<unsigned char> host(size);
  CHECK(cuMemsetD8(a, 0x5a, size));
  CHECK(cuCtxSynchronize());
  CHECK(cuMemcpyDtoH(host.data(), b, size));
  const bool forward = std::all_of(host.begin(), host.end(),
                                 [](auto x) { return x == 0x5a; });
  CHECK(cuMemsetD8(b, 0xa5, size));
  CHECK(cuCtxSynchronize());
  CHECK(cuMemcpyDtoH(host.data(), a, size));
  const bool reverse = std::all_of(host.begin(), host.end(),
                                 [](auto x) { return x == 0xa5; });
  std::printf("{\"granularity\":%zu,\"bytes\":%zu,\"a\":%llu,\"b\":%llu,"
              "\"same_retained_handle\":%s,\"forward\":%s,\"reverse\":%s}\n",
              granularity, size, a, b, same_handle ? "true" : "false",
              forward ? "true" : "false", reverse ? "true" : "false");
  CHECK(cuMemUnmap(a, size));
  CHECK(cuMemUnmap(b, size));
  CHECK(cuMemAddressFree(a, size));
  CHECK(cuMemAddressFree(b, size));
  CHECK(cuMemRelease(handle));
  CHECK(cuDevicePrimaryCtxRelease(device));
  return same_handle && forward && reverse ? 0 : 3;
}
