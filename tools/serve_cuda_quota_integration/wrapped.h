#pragma once

#include <cuda_runtime_api.h>

extern "C" cudaError_t __wrap_cudaMalloc(void**, size_t);
extern "C" cudaError_t __wrap_cudaMallocManaged(void**, size_t, unsigned);
extern "C" cudaError_t __wrap_cudaHostAlloc(void**, size_t, unsigned);
extern "C" cudaError_t __wrap_cudaMallocHost(void**, size_t);
extern "C" cudaError_t __wrap_cudaMallocAsync(void**, size_t, cudaStream_t);
extern "C" cudaError_t __wrap_cudaFree(void*);
extern "C" cudaError_t __wrap_cudaFreeHost(void*);
extern "C" cudaError_t __wrap_cudaFreeAsync(void*, cudaStream_t);
extern "C" cudaError_t __wrap_cudaStreamCreate(cudaStream_t*);
extern "C" cudaError_t __wrap_cudaStreamCreateWithFlags(cudaStream_t*,
                                                        unsigned);
extern "C" cudaError_t __wrap_cudaStreamCreateWithPriority(cudaStream_t*,
                                                           unsigned, int);
extern "C" cudaError_t __wrap_cudaStreamDestroy(cudaStream_t);
extern "C" cudaError_t __wrap_cudaStreamSynchronize(cudaStream_t);
extern "C" cudaError_t __wrap_cudaDeviceSynchronize();
extern "C" cudaError_t __wrap_cudaEventSynchronize(cudaEvent_t);
extern "C" cudaError_t __wrap_cudaEventDestroy(cudaEvent_t);
