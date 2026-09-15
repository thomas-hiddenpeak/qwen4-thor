// Step 1a: verify a C++ binary (no Python) drives a CuTe DSL AOT kernel.
// Links vec_add.o (host launch entry + cubin) + libcute_dsl_runtime.
#include "vec_add.h"
#include <cuda_runtime.h>
#include <cstdio>
#include <cstdlib>
#include <vector>

int main() {
  const int n = 4096;
  float *x = nullptr, *y = nullptr, *out = nullptr;
  cudaMalloc(&x, n * 4);
  cudaMalloc(&y, n * 4);
  cudaMalloc(&out, n * 4);
  std::vector<float> hx(n, 1.0f), hy(n, 2.0f);
  cudaMemcpy(x, hx.data(), n * 4, cudaMemcpyHostToDevice);
  cudaMemcpy(y, hy.data(), n * 4, cudaMemcpyHostToDevice);
  cudaMemset(out, 0, n * 4);

  vec_add_Kernel_Module_t module;
  vec_add_Kernel_Module_Load(&module);
  int32_t ret = cute_dsl_vec_add_wrapper(&module, x, y, out, n);
  if (ret != 0) {
    printf("wrapper ret=%d\n", ret);
    return 1;
  }
  cudaDeviceSynchronize();

  std::vector<float> ho(n);
  cudaMemcpy(ho.data(), out, n * 4, cudaMemcpyDeviceToHost);
  int bad = 0;
  for (int i = 0; i < n; ++i)
    if (ho[i] != 3.0f)
      ++bad;
  printf("vec_add AOT (no Python): n=%d bad=%d (expect 0)\n", n, bad);
  vec_add_Kernel_Module_Unload(&module);
  cudaFree(x);
  cudaFree(y);
  cudaFree(out);
  return bad != 0;
}
