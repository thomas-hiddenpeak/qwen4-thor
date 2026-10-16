#include <cuda_runtime.h>
#include <cassert>
#include <cstdio>
__global__ void ModuleObserverCalibration(int* p) { *p=42; }
int main() {
 int* p=nullptr;assert(cudaMalloc(&p,sizeof(int))==cudaSuccess);
 ModuleObserverCalibration<<<1,1>>>(p);
 assert(cudaDeviceSynchronize()==cudaSuccess);
 int value=0;assert(cudaMemcpy(&value,p,sizeof(int),cudaMemcpyDeviceToHost)==cudaSuccess);
 assert(value==42);assert(cudaFree(p)==cudaSuccess);
 std::puts("calibration_result=42");
}
