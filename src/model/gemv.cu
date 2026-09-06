// M=1 BF16 GEMV: y[N] = sum_k x[k] * W[N, K] (row-major W [N, K], no bias).
//
// Decode (T=1) turns every BF16 projection into a GEMV. cuBLASLt's GEMM
// kernels are not bandwidth-optimal for M=1 (they tile over M and leave the
// tensor cores / DRAM underutilized), so this dedicated kernel reads W once
// at full DRAM bandwidth and accumulates in FP32. It is dispatched from
// q4t::model::Bf16Gemm when M == 1 (see linear.h); the result must match the
// cuBLASLt path to BF16 storage precision (FP32 accumulation, single
// rounding to BF16).
#include <cuda_bf16.h>
#include <cuda_runtime.h>

#include <cstdint>

namespace q4t {
namespace model {
namespace {

constexpr int kGevThreads = 256;

// Each thread accumulates a strided slice of the K dot product for one output
// row, then atomically adds alpha*acc into the FP32 scratch. W rows are read
// contiguously (coalesced 16B loads); x is small (K*2 bytes) and L2-resident
// across threads. The FP32 scratch is converted to BF16 in a second pass.
__global__ void Bf16GevKernel(const uint16_t* __restrict__ w,
                              const uint16_t* __restrict__ x,
                              uint16_t* __restrict__ y, int N, int K,
                              float alpha) {
  const int n = blockIdx.x;
  if (n >= N) return;
  const uint16_t* wrow = w + (size_t)n * K;

  float acc = 0.f;
  const int vec = K / 8;  // full 16B chunks
  for (int i = threadIdx.x; i < vec; i += blockDim.x) {
    const uint4 v = *reinterpret_cast<const uint4*>(wrow + i * 8);
    const uint16_t* wb = reinterpret_cast<const uint16_t*>(&v);
#pragma unroll
    for (int j = 0; j < 8; ++j) {
      // Bit-cast the BF16 pattern to float (NOT a value conversion: the
      // uint16 IS the BF16 bit pattern, e.g. 0x3F80 == 1.0f).
      acc += __bfloat162float(__ushort_as_bfloat16(wb[j])) *
             __bfloat162float(__ushort_as_bfloat16(x[i * 8 + j]));
    }
  }
  // Tail (K % 8 elements).
  for (int i = vec * 8 + threadIdx.x; i < K; i += blockDim.x) {
    acc += __bfloat162float(__ushort_as_bfloat16(wrow[i])) *
           __bfloat162float(__ushort_as_bfloat16(x[i]));
  }
  // Block reduce (deterministic order — no atomicAdd, which is non-deterministic
  // across runs and would flip near-tie argmax between the legacy and seq
  // decode paths). One block owns output row n, so a direct write is safe.
  __shared__ float s[32];
  for (int off = 16; off > 0; off >>= 1)
    acc += __shfl_down_sync(0xffffffff, acc, off);
  const int lane = threadIdx.x & 31;
  const int wid = threadIdx.x >> 5;
  if (lane == 0) s[wid] = acc;
  __syncthreads();
  if (threadIdx.x == 0) {
    float total = 0.f;
    for (int i = 0; i < (blockDim.x >> 5); ++i) total += s[i];
    y[n] = __bfloat16_as_ushort(__float2bfloat16_rn(alpha * total));
  }
}

}  // namespace

// y[M=1, N] = alpha * sum_k x[k] * W[N, K] (beta must be 0, caller-checked).
// Returns false if the shape is unsupported (caller falls back to GEMM).
bool Bf16Gev(const uint16_t* x, const uint16_t* w, uint16_t* y, int N, int K,
             float alpha, cudaStream_t stream) {
  if (N <= 0 || K <= 0 || (K & 7) != 0) return false;
  // One block per output row (N blocks); 256 threads cooperate on the K dot
  // product. Grid = N is fine for large N (lm_head N=248320).
  Bf16GevKernel<<<N, kGevThreads, 0, stream>>>(w, x, y, N, K, alpha);
  return cudaGetLastError() == cudaSuccess;
}

}  // namespace model
}  // namespace q4t
