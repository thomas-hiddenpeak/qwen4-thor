// Standalone reliability probe; never linked into the production binary.
#include <cuda.h>
#include <algorithm>
#include <cstdio>
#include <cstdlib>
#include <vector>

void Check(CUresult r, const char* op) {
  if (r == CUDA_SUCCESS) return;
  const char* message = nullptr;
  cuGetErrorString(r, &message);
  std::fprintf(stderr, "%s: %d %s\n", op, int(r), message ? message : "unknown");
  std::exit(1);
}
int main() {
  Check(cuInit(0), "init");
  CUdevice device;
  Check(cuDeviceGet(&device, 0), "device");
  int supported = 0;
  Check(cuDeviceGetAttribute(&supported,
      CU_DEVICE_ATTRIBUTE_VIRTUAL_MEMORY_MANAGEMENT_SUPPORTED, device), "VMM");
  std::printf("vmm_supported=%d\n", supported);
  std::fflush(stdout);
  if (!supported) return 2;
  CUcontext context;
  Check(cuDevicePrimaryCtxRetain(&context, device), "retain");
  Check(cuCtxSetCurrent(context), "context");
  CUmemAllocationProp prop{};
  prop.type = CU_MEM_ALLOCATION_TYPE_PINNED;
  prop.location.type = CU_MEM_LOCATION_TYPE_DEVICE;
  prop.location.id = device;
  size_t minimum = 0, recommended = 0;
  Check(cuMemGetAllocationGranularity(&minimum, &prop,
      CU_MEM_ALLOC_GRANULARITY_MINIMUM), "minimum");
  Check(cuMemGetAllocationGranularity(&recommended, &prop,
      CU_MEM_ALLOC_GRANULARITY_RECOMMENDED), "recommended");
  std::printf("minimum=%zu recommended=%zu\n", minimum, recommended);
  std::fflush(stdout);
  if (minimum == 0 || minimum > (64u << 20)) return 3;
  // Two independent handles, one contiguous virtual range; repeated once
  // after a controlled partial-construction rollback.
  for (int trial = 0; trial < 3; ++trial) {
    CUdeviceptr base;
    Check(cuMemAddressReserve(&base, 2 * minimum, 0, 0, 0), "reserve");
    int mapped = 0;
    for (int part = 0; part < (trial == 0 ? 1 : 2); ++part) {
      CUmemGenericAllocationHandle handle;
      Check(cuMemCreate(&handle, minimum, &prop, 0), "create");
      Check(cuMemMap(base + part * minimum, minimum, 0, handle, 0), "map");
      ++mapped;
      Check(cuMemRelease(handle), "release mapped handle");
    }
    if (trial != 0) {
      CUmemAccessDesc access{};
      access.location = prop.location;
      access.flags = CU_MEM_ACCESS_FLAGS_PROT_READWRITE;
      Check(cuMemSetAccess(base, 2 * minimum, &access, 1), "access");
      std::vector<unsigned char> input(64), output(64);
      for (size_t i = 0; i < input.size(); ++i) input[i] = (i * 17 + trial) % 256;
      // One transfer crosses the physical-handle mapping boundary.
      Check(cuMemcpyHtoD(base + minimum - 32, input.data(), input.size()), "H2D");
      Check(cuMemcpyDtoH(output.data(), base + minimum - 32, output.size()), "D2H");
      if (input != output) return 4;
    }
    Check(cuCtxSynchronize(), "sync before unmap");
    for (int part = mapped - 1; part >= 0; --part)
      Check(cuMemUnmap(base + part * minimum, minimum), "unmap");
    Check(cuMemAddressFree(base, 2 * minimum), "free address");
    std::printf("trial=%d %s PASS\n", trial,
                trial == 0 ? "partial construction rollback" : "cross-boundary IO");
  }
  Check(cuDevicePrimaryCtxRelease(device), "release context");
}
