// Internal bridge: startup owner and public link wrappers share these calls.
#pragma once

#include <cuda_runtime_api.h>

namespace q4t::runtime {
cudaError_t QuotaCudaMalloc(void**, size_t);
cudaError_t QuotaCudaMallocManaged(void**, size_t, unsigned int);
cudaError_t QuotaCudaHostAlloc(void**, size_t, unsigned int);
cudaError_t QuotaCudaMallocHost(void**, size_t);
cudaError_t QuotaCudaMallocAsync(void**, size_t, cudaStream_t);
cudaError_t QuotaCudaFree(void*);
cudaError_t QuotaCudaFreeHost(void*);
cudaError_t QuotaCudaFreeAsync(void*, cudaStream_t);
cudaError_t QuotaCudaStreamCreate(cudaStream_t*, unsigned int, int, int);
cudaError_t QuotaCudaStreamDestroy(cudaStream_t);
cudaError_t QuotaCudaStreamSynchronize(cudaStream_t);
cudaError_t QuotaCudaDeviceSynchronize();
}  // namespace q4t::runtime
