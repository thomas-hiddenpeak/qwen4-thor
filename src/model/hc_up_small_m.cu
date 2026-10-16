// Fixed-shape HC up projection: two independent output columns per warp.
// Preserve the production scattered small-M kernel's per-output arithmetic.
#include <cuda_bf16.h>
#include <cuda_runtime.h>

#include <cstdint>
#include <cstring>

namespace q4t::model {
namespace {

__device__ __forceinline__ void FmaPair(uint64_t& acc, const float2& a,
                                      const float2& b) {
  asm volatile("fma.rn.f32x2 %0, %1, %2, %0;"
               : "+l"(acc)
               : "l"(reinterpret_cast<const uint64_t&>(a)),
                 "l"(reinterpret_cast<const uint64_t&>(b)));
}

__global__ void HcUpFourRowsKernel(const uint16_t* __restrict__ w,
                                 const uint16_t* __restrict__ x,
                                 uint16_t* __restrict__ y, float alpha) {
  constexpr int kK = 320;
  constexpr int kN = 10240;
  constexpr int kBlocks = kN / 16;
  __shared__ __align__(16) uint16_t sx[4 * kK];
  auto* sv = reinterpret_cast<float4*>(sx);
  const auto* xv = reinterpret_cast<const float4*>(x);
  for (int i = threadIdx.x; i < 4 * kK / 8; i += 256) sv[i] = xv[i];
  __syncthreads();

  const int lane = threadIdx.x % 32;
  const int warp = threadIdx.x / 32;
  uint64_t acc[2][4] = {};
  // For each output, lane i consumes vector i, then i+32, exactly as before.
  for (int i = lane; i < kK / 8; i += 32) {
    float2 xf[4][4];
#pragma unroll
    for (int m = 0; m < 4; ++m) {
      const float4 xr = sv[m * (kK / 8) + i];
      const auto* x2 = reinterpret_cast<const __nv_bfloat162*>(&xr);
#pragma unroll
      for (int j = 0; j < 4; ++j) xf[m][j] = __bfloat1622float2(x2[j]);
    }
#pragma unroll
    for (int c = 0; c < 2; ++c) {
      const int n = blockIdx.x + (warp + c * 8) * kBlocks;
      const auto* wv = reinterpret_cast<const float4*>(w + n * kK);
      const float4 wr = wv[i];
      const auto* w2 = reinterpret_cast<const __nv_bfloat162*>(&wr);
#pragma unroll
      for (int j = 0; j < 4; ++j) {
        const float2 wf = __bfloat1622float2(w2[j]);
#pragma unroll
        for (int m = 0; m < 4; ++m) FmaPair(acc[c][m], wf, xf[m][j]);
      }
    }
  }
#pragma unroll
  for (int c = 0; c < 2; ++c) {
    const int n = blockIdx.x + (warp + c * 8) * kBlocks;
#pragma unroll
    for (int m = 0; m < 4; ++m) {
      float2 pair;
      std::memcpy(&pair, &acc[c][m], sizeof(pair));
      float sum = pair.x + pair.y;
#pragma unroll
      for (int offset = 16; offset > 0; offset /= 2)
        sum += __shfl_down_sync(0xffffffff, sum, offset);
      if (lane == 0)
        y[m * kN + n] = __bfloat16_as_ushort(__float2bfloat16_rn(sum * alpha));
    }
  }
}

}  // namespace

bool HcUpFourRows(const uint16_t* x, const uint16_t* w, uint16_t* y,
                 float alpha, cudaStream_t stream) {
  HcUpFourRowsKernel<<<640, 256, 0, stream>>>(w, x, y, alpha);
  return cudaGetLastError() == cudaSuccess;
}

}  // namespace q4t::model
