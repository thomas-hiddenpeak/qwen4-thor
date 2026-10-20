// Research-only VMM owner. Callers synchronize all users before Free().
#pragma once
#include <cuda.h>
#include <cstddef>
#include <cstdlib>
#include <limits>

struct VmmBlock {
  CUdeviceptr address = 0;
  size_t size = 0;
  size_t mapped = 0;
  size_t chunk = 0;

  CUresult Free() {
    while (mapped) {
      auto rc = cuMemUnmap(address + mapped - chunk, chunk);
      if (rc != CUDA_SUCCESS) return rc;  // Retain remaining ownership.
      mapped -= chunk;
    }
    if (address) {
      auto rc = cuMemAddressFree(address, size);
      if (rc != CUDA_SUCCESS) return rc;
      address = 0;
      size = 0;
    }
    return CUDA_SUCCESS;
  }

  CUresult Allocate(size_t bytes, int device, int fail_after = -1) {
    if (address || !bytes) return CUDA_ERROR_INVALID_VALUE;
    CUmemAllocationProp prop{};
    prop.type = CU_MEM_ALLOCATION_TYPE_PINNED;
    prop.location.type = CU_MEM_LOCATION_TYPE_DEVICE;
    prop.location.id = device;
    auto rc = cuMemGetAllocationGranularity(
        &chunk, &prop, CU_MEM_ALLOC_GRANULARITY_MINIMUM);
    if (rc != CUDA_SUCCESS) return rc;
    // Frozen prototype: no silent change to allocation granularity.
    if (chunk != (2u << 20)) return CUDA_ERROR_NOT_SUPPORTED;
    if (bytes > std::numeric_limits<size_t>::max() - (chunk - 1))
      return CUDA_ERROR_INVALID_VALUE;
    size = ((bytes + chunk - 1) / chunk) * chunk;
    rc = cuMemAddressReserve(&address, size, 0, 0, 0);
    if (rc != CUDA_SUCCESS) return rc;
    while (mapped < size) {
      if (fail_after >= 0 && mapped / chunk == size_t(fail_after)) {
        rc = CUDA_ERROR_OUT_OF_MEMORY;
        break;
      }
      CUmemGenericAllocationHandle handle;
      rc = cuMemCreate(&handle, chunk, &prop, 0);
      if (rc != CUDA_SUCCESS) break;
      auto map_rc = cuMemMap(address + mapped, chunk, 0, handle, 0);
      if (map_rc == CUDA_SUCCESS) mapped += chunk;
      auto release_rc = cuMemRelease(handle);
      // A failed handle release cannot safely lose ownership. End this
      // diagnostic process rather than pretending cleanup succeeded.
      if (release_rc != CUDA_SUCCESS) std::abort();
      rc = map_rc;
      if (rc != CUDA_SUCCESS) break;
    }
    if (rc == CUDA_SUCCESS) {
      CUmemAccessDesc access{};
      access.location = prop.location;
      access.flags = CU_MEM_ACCESS_FLAGS_PROT_READWRITE;
      rc = cuMemSetAccess(address, size, &access, 1);
    }
    if (rc != CUDA_SUCCESS) {
      auto cleanup = Free();
      if (cleanup != CUDA_SUCCESS) return cleanup;
    }
    return rc;
  }
};
