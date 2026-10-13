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
                                uint16_t* __restrict__ y, int K, float alpha,
                                bool apply_gate = false) {
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
  if (lane == 0) {
    uint16_t value = __bfloat16_as_ushort(__float2bfloat16_rn(alpha * sum));
    if (apply_gate) {
      // Preserve the materialized BF16 projection before evaluating the gate.
      const float v = __bfloat162float(__ushort_as_bfloat16(value)) * 0.25f;
      value = __bfloat16_as_ushort(
          __float2bfloat16_rn(2.0f / (1.0f + __expf(-v))));
    }
    y[blockIdx.x] = value;
  }
}

}  // namespace

// Called only for N=4, K=10240 and 16-byte-aligned input/weight pointers.
bool HcInjectGev(const uint16_t* x, const uint16_t* w, uint16_t* y,
                 float alpha, cudaStream_t stream) {
  HcInjectGevKernel<<<4, 32, 0, stream>>>(w, x, y, 10240, alpha);
  return cudaGetLastError() == cudaSuccess;
}

// Multi-row (T>1) variant of the fused HC inject GEMV: one warp per (t, b)
// output, so each row is computed with the EXACT per-lane accumulation and
// shuffle order of HcInjectGevKernel (bit-identical to the T=1 GEMV on the
// same input row). Used by the MTP verify path (T=k+1, small T) where the
// cuBLASLt GEMM costs ~129 us/call on this degenerate 4x4x10240 shape while
// this kernel streams ~320 KB/call.
__global__ void HcInjectGevMultirowKernel(const uint16_t* __restrict__ w,
                                          const uint16_t* __restrict__ x,
                                          uint16_t* __restrict__ y, int T,
                                          int K, float alpha,
                                          bool apply_gate) {
  const int lane = threadIdx.x;
  const int t = blockIdx.x / 4;  // row (token)
  const int b = blockIdx.x % 4;  // branch
  if (t >= T) return;
  const uint16_t* wrow = w + static_cast<size_t>(b) * K;
  const uint16_t* xrow = x + static_cast<size_t>(t) * K;
  const float4* w_v4 = reinterpret_cast<const float4*>(wrow);
  const float4* x_v4 = reinterpret_cast<const float4*>(xrow);
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
      const float2 bb = __bfloat1622float2(w2[j]);
      asm volatile("fma.rn.f32x2 %0, %1, %2, %0;"
                   : "+l"(acc)
                   : "l"(reinterpret_cast<const uint64_t&>(a)),
                     "l"(reinterpret_cast<const uint64_t&>(bb)));
    }
  }
  float2 v;
  std::memcpy(&v, &acc, sizeof(v));
  float sum = v.x + v.y;
#pragma unroll
  for (int off = 16; off > 0; off >>= 1)
    sum += __shfl_down_sync(0xffffffff, sum, off);
  if (lane == 0) {
    uint16_t value = __bfloat16_as_ushort(__float2bfloat16_rn(alpha * sum));
    if (apply_gate) {
      // Preserve the materialized BF16 projection before evaluating the gate
      // (same order as the T=1 fused path and ApplyInjectGateKernel).
      const float gv = __bfloat162float(__ushort_as_bfloat16(value)) * 0.25f;
      value = __bfloat16_as_ushort(
          __float2bfloat16_rn(2.0f / (1.0f + __expf(-gv))));
    }
    y[static_cast<size_t>(t) * 4 + b] = value;
  }
}

// Multi-row fused HC inject gate for 2 <= T <= 8, N=4, K=10240, with
// 16-byte-aligned input/weight pointers. Each row is bit-identical to
// HcInjectGevGated on the same row.
bool HcInjectGevGatedMultirow(const uint16_t* x, const uint16_t* w,
                              uint16_t* y, int T, cudaStream_t stream) {
  if (T < 2 || T > 8) return false;
  HcInjectGevMultirowKernel<<<T * 4, 32, 0, stream>>>(w, x, y, T, 10240, 1.0f,
                                                      true);
  return cudaGetLastError() == cudaSuccess;
}

// HC count is four; return the same BF16 gate as ApplyInjectGateKernel.
bool HcInjectGevGated(const uint16_t* x, const uint16_t* w, uint16_t* y,
                     cudaStream_t stream) {
  HcInjectGevKernel<<<4, 32, 0, stream>>>(w, x, y, 10240, 1.0f, true);
  return cudaGetLastError() == cudaSuccess;
}

}  // namespace model
}  // namespace q4t
