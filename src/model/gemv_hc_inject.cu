// HC injection decode projection: four outputs, one warp/block per output.
// Keep the original BF16 GEMV lane accumulation and shuffle order; only
// distribute the rows across CTAs and read x directly instead of staging it.
#include <cuda_bf16.h>
#include <cuda_runtime.h>

#include <cstdint>
#include <cstring>

namespace q4t {
namespace model {
namespace {

__global__ void HcInjectGevKernel(const uint16_t* __restrict__ w,
                                const uint16_t* __restrict__ x,
                                uint16_t* __restrict__ y, int K, float alpha) {
  const int lane = threadIdx.x;
  const uint16_t* wrow = w + static_cast<size_t>(blockIdx.x) * K;
  const float4* w_v4 = reinterpret_cast<const float4*>(wrow);
  const float4* x_v4 = reinterpret_cast<const float4*>(x);
  const int k8 = K / 8;
  uint64_t acc = 0;
  for (int i = lane; i < k8; i += 32) {
    const float4 w4 = w_v4[i];
    const float4 x4 = x_v4[i];
    const auto* w2 = reinterpret_cast<const __nv_bfloat162*>(&w4);
    const auto* x2 = reinterpret_cast<const __nv_bfloat162*>(&x4);
#pragma unroll
    for (int j = 0; j < 4; ++j) {
      const float2 a = __bfloat1622float2(x2[j]);
      const float2 b = __bfloat1622float2(w2[j]);
      asm volatile("fma.rn.f32x2 %0, %1, %2, %0;"
                   : "+l"(acc)
                   : "l"(reinterpret_cast<const uint64_t&>(a)),
                     "l"(reinterpret_cast<const uint64_t&>(b)));
    }
  }
  float2 v;
  std::memcpy(&v, &acc, sizeof(v));
  float sum = v.x + v.y;
#pragma unroll
  for (int off = 16; off > 0; off >>= 1)
    sum += __shfl_down_sync(0xffffffff, sum, off);
  if (lane == 0)
    y[blockIdx.x] = __bfloat16_as_ushort(__float2bfloat16_rn(alpha * sum));
}

}  // namespace

// Called only for N=4, K=10240 and 16-byte-aligned input/weight pointers.
bool HcInjectGev(const uint16_t* x, const uint16_t* w, uint16_t* y,
                 float alpha, cudaStream_t stream) {
  HcInjectGevKernel<<<4, 32, 0, stream>>>(w, x, y, 10240, alpha);
  return cudaGetLastError() == cudaSuccess;
}

}  // namespace model
}  // namespace q4t
