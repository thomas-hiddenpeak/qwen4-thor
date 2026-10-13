// HC inject GEMV bench: T=4 multi-row (MTP verify) and T=1 (decode) shapes.
// N=4 outputs per row, K=10240, BF16. Measures per-call time with L2 defeat
// (256 MB memset between calls, same methodology as small_m_gemm_v3_bench).
//
// Variants (multi-row, T rows x 4 branches):
//   V0 : production HcInjectGevMultirowKernel (T*4 blocks x 32 threads)
//   V1 : 1 warp per output, 4x-unrolled K loop (BIT-IDENTICAL to V0)
//   V2 : 4 warps per output, K/4 split, smem reduce (T*4 blocks x 128)
//   V3 : 8 warps per output, K/8 split (T*4 blocks x 256)
//   V4 : 16 warps per output, K/16 split (T*4 blocks x 512)
// Build: q4t_hc_inject_bench
#include <cuda_bf16.h>
#include <cuda_runtime.h>

#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <vector>

namespace {

#define CK(x)                                                     \
  do {                                                            \
    cudaError_t e = (x);                                          \
    if (e != cudaSuccess) {                                       \
      std::printf("CUDA error %s:%d: %s\n", __FILE__, __LINE__,   \
                  cudaGetErrorString(e));                         \
      std::exit(1);                                               \
    }                                                             \
  } while (0)

__device__ __forceinline__ void F32x2Fma(uint64_t& acc, const float2& a,
                                         const float2& b) {
  asm volatile("fma.rn.f32x2 %0, %1, %2, %0;"
               : "+l"(acc)
               : "l"(reinterpret_cast<const uint64_t&>(a)),
                 "l"(reinterpret_cast<const uint64_t&>(b)));
}
__device__ __forceinline__ float F32x2Reduce(uint64_t acc) {
  float2 v;
  std::memcpy(&v, &acc, 8);
  return v.x + v.y;
}

constexpr int kK = 10240;
constexpr int kK8 = kK / 8;

// V0: production multi-row kernel (copied verbatim from
// src/model/gemv_hc_inject.cu, gate omitted — gate is a scalar op).
__global__ void V0Multirow(const uint16_t* __restrict__ w,
                           const uint16_t* __restrict__ x, uint16_t* __restrict__ y,
                           int T) {
  const int lane = threadIdx.x;
  const int t = blockIdx.x / 4;
  const int b = blockIdx.x % 4;
  if (t >= T) return;
  const float4* w_v4 = reinterpret_cast<const float4*>(w + (size_t)b * kK);
  const float4* x_v4 = reinterpret_cast<const float4*>(x + (size_t)t * kK);
  uint64_t acc = 0;
  for (int i = lane; i < kK8; i += 32) {
    const float4 w4 = w_v4[i];
    const float4 x4 = x_v4[i];
    const auto* w2 = reinterpret_cast<const __nv_bfloat162*>(&w4);
    const auto* x2 = reinterpret_cast<const __nv_bfloat162*>(&x4);
#pragma unroll
    for (int j = 0; j < 4; ++j)
      F32x2Fma(acc, __bfloat1622float2(x2[j]), __bfloat1622float2(w2[j]));
  }
  float sum = F32x2Reduce(acc);
#pragma unroll
  for (int off = 16; off > 0; off >>= 1)
    sum += __shfl_down_sync(0xffffffff, sum, off);
  if (lane == 0) y[(size_t)t * 4 + b] = __bfloat16_as_ushort(__float2bfloat16_rn(sum));
}

// V1: same per-lane partial sums as V0 (bit-identical), but the K loop is
// unrolled 4x so each lane issues 8 independent 16B loads before consuming.
__global__ void V1Multirow(const uint16_t* __restrict__ w,
                           const uint16_t* __restrict__ x, uint16_t* __restrict__ y,
                           int T) {
  const int lane = threadIdx.x;
  const int t = blockIdx.x / 4;
  const int b = blockIdx.x % 4;
  if (t >= T) return;
  const float4* w_v4 = reinterpret_cast<const float4*>(w + (size_t)b * kK);
  const float4* x_v4 = reinterpret_cast<const float4*>(x + (size_t)t * kK);
  uint64_t acc = 0;
  const int n4 = kK8 / 4 / 32;  // 10 chunks of 4 stride-32 steps
  for (int c = 0; c < n4; ++c) {
    const int i0 = lane + c * 128;
    float4 w4[4], x4[4];
#pragma unroll
    for (int u = 0; u < 4; ++u) {
      w4[u] = w_v4[i0 + u * 32];
      x4[u] = x_v4[i0 + u * 32];
    }
#pragma unroll
    for (int u = 0; u < 4; ++u) {
      const auto* w2 = reinterpret_cast<const __nv_bfloat162*>(&w4[u]);
      const auto* x2 = reinterpret_cast<const __nv_bfloat162*>(&x4[u]);
#pragma unroll
      for (int j = 0; j < 4; ++j)
        F32x2Fma(acc, __bfloat1622float2(x2[j]), __bfloat1622float2(w2[j]));
    }
  }
  float sum = F32x2Reduce(acc);
#pragma unroll
  for (int off = 16; off > 0; off >>= 1)
    sum += __shfl_down_sync(0xffffffff, sum, off);
  if (lane == 0) y[(size_t)t * 4 + b] = __bfloat16_as_ushort(__float2bfloat16_rn(sum));
}

// V2/V3/V4: S warps per output split K into S contiguous chunks; each warp
// keeps the stride-32 lane pattern inside its chunk; cross-warp reduce via
// smem in warp order. Deterministic but NOT bit-identical to V0.
template <int S>
__global__ void VKSplit(const uint16_t* __restrict__ w,
                        const uint16_t* __restrict__ x, uint16_t* __restrict__ y,
                        int T) {
  constexpr int kWarp = 32;
  const int lane = threadIdx.x & (kWarp - 1);
  const int wid = threadIdx.x / kWarp;
  const int t = blockIdx.x / 4;
  const int b = blockIdx.x % 4;
  if (t >= T) return;
  const float4* w_v4 = reinterpret_cast<const float4*>(w + (size_t)b * kK);
  const float4* x_v4 = reinterpret_cast<const float4*>(x + (size_t)t * kK);
  const int chunk = kK8 / S;  // float4 slots per warp
  uint64_t acc = 0;
  const int base = wid * chunk;
  for (int i = base + lane; i < base + chunk; i += kWarp) {
    const float4 w4 = w_v4[i];
    const float4 x4 = x_v4[i];
    const auto* w2 = reinterpret_cast<const __nv_bfloat162*>(&w4);
    const auto* x2 = reinterpret_cast<const __nv_bfloat162*>(&x4);
#pragma unroll
    for (int j = 0; j < 4; ++j)
      F32x2Fma(acc, __bfloat1622float2(x2[j]), __bfloat1622float2(w2[j]));
  }
  float sum = F32x2Reduce(acc);
#pragma unroll
  for (int off = 16; off > 0; off >>= 1)
    sum += __shfl_down_sync(0xffffffff, sum, off);
  __shared__ float part[S];
  if (lane == 0) part[wid] = sum;
  __syncthreads();
  if (threadIdx.x == 0) {
    float s = 0;
#pragma unroll
    for (int u = 0; u < S; ++u) s += part[u];
    y[(size_t)t * 4 + b] = __bfloat16_as_ushort(__float2bfloat16_rn(s));
  }
}

uint16_t* g_l2killer = nullptr;
constexpr size_t kL2KillerBytes = 256u << 20;

template <typename Fn>
float TimeIt(Fn fn, int reps) {
  cudaEvent_t e0, e1;
  CK(cudaEventCreate(&e0));
  CK(cudaEventCreate(&e1));
  float total = 0;
  for (int it = 0; it < reps; ++it) {
    CK(cudaMemsetAsync(g_l2killer, 0, kL2KillerBytes, 0));
    CK(cudaStreamSynchronize(0));
    CK(cudaEventRecord(e0, 0));
    fn();
    CK(cudaEventRecord(e1, 0));
    CK(cudaEventSynchronize(e1));
    float ms = 0;
    CK(cudaEventElapsedTime(&ms, e0, e1));
    if (it > 2) total += ms;
  }
  cudaEventDestroy(e0);
  cudaEventDestroy(e1);
  return total / (reps - 3);  // ms per call
}

void CheckBitwise(const uint16_t* a, const uint16_t* b, int n,
                  const char* name) {
  std::vector<uint16_t> ha(n), hb(n);
  CK(cudaMemcpy(ha.data(), a, n * 2, cudaMemcpyDeviceToHost));
  CK(cudaMemcpy(hb.data(), b, n * 2, cudaMemcpyDeviceToHost));
  int diff = 0;
  for (int i = 0; i < n; ++i)
    if (ha[i] != hb[i]) ++diff;
  std::printf("  %-8s vs V0: %d/%d differ%s\n", name, diff, n,
              diff == 0 ? " (bit-identical)" : "");
}

}  // namespace

int main() {
  CK(cudaMalloc(&g_l2killer, kL2KillerBytes));
  const int T = 4;
  uint16_t* w;
  uint16_t* x;
  uint16_t* y0;
  uint16_t* yv;
  CK(cudaMalloc(&w, (size_t)4 * kK * 2));
  CK(cudaMalloc(&x, (size_t)T * kK * 2));
  CK(cudaMalloc(&y0, 64));
  CK(cudaMalloc(&yv, 64));
  // Random-ish data.
  {
    std::vector<uint16_t> hw(4 * kK), hx(T * kK);
    uint32_t seed = 12345;
    auto rng = [&]() { seed = seed * 1664525u + 1013904223u; return (seed >> 8) & 0x7fff; };
    for (auto& v : hw) v = rng();
    for (auto& v : hx) v = rng();
    CK(cudaMemcpy(w, hw.data(), hw.size() * 2, cudaMemcpyHostToDevice));
    CK(cudaMemcpy(x, hx.data(), hx.size() * 2, cudaMemcpyHostToDevice));
  }

  std::printf("HC inject bench: T=%d N=4 K=%d BF16, L2-defeated, 30 reps\n", T, kK);
  std::printf("per-call traffic: w=80KB x=%zuKB floor@246GB/s=%.2fus\n",
              (size_t)T * kK / 1024, (80.0 + (double)T * kK / 1024) / 246.0);

  auto t0 = TimeIt([&] {
    V0Multirow<<<T * 4, 32>>>(w, x, y0, T);
  }, 30);
  std::printf("V0 prod  (T*4 x 32):  %7.2f us/call\n", t0 * 1000);

  auto t1 = TimeIt([&] {
    V1Multirow<<<T * 4, 32>>>(w, x, yv, T);
  }, 30);
  std::printf("V1 unroll4 (T*4 x 32): %7.2f us/call\n", t1 * 1000);
  CheckBitwise(yv, y0, T * 4, "V1");

  auto t2 = TimeIt([&] {
    VKSplit<4><<<T * 4, 128>>>(w, x, yv, T);
  }, 30);
  std::printf("V2 ksplit4 (T*4 x 128): %7.2f us/call\n", t2 * 1000);
  CheckBitwise(yv, y0, T * 4, "V2");

  auto t3 = TimeIt([&] {
    VKSplit<8><<<T * 4, 256>>>(w, x, yv, T);
  }, 30);
  std::printf("V3 ksplit8 (T*4 x 256): %7.2f us/call\n", t3 * 1000);
  CheckBitwise(yv, y0, T * 4, "V3");

  auto t4 = TimeIt([&] {
    VKSplit<16><<<T * 4, 512>>>(w, x, yv, T);
  }, 30);
  std::printf("V4 ksplit16 (T*4 x 512): %7.2f us/call\n", t4 * 1000);
  CheckBitwise(yv, y0, T * 4, "V4");

  // T=1 decode path (4 blocks x 32).
  auto d0 = TimeIt([&] {
    V0Multirow<<<4, 32>>>(w, x, y0, 1);
  }, 30);
  std::printf("T=1 prod (4 x 32):     %7.2f us/call\n", d0 * 1000);
  auto d1 = TimeIt([&] {
    V1Multirow<<<4, 32>>>(w, x, yv, 1);
  }, 30);
  std::printf("T=1 unroll4 (4 x 32):  %7.2f us/call\n", d1 * 1000);
  CheckBitwise(yv, y0, 4, "T1-V1");
  return 0;
}
