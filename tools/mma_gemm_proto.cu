// Standalone validation of a generic bf16 tensor-core GEMM using the same
// m16n8k16 mma.sync fragment packing as full_attention.cu (proven pattern).
//   C[M,N] = A[M,K] @ B[K,N]
// A stored row-major [M,K]; the mma B operand is [K,N] "col", which equals
// Bt[N,K] row-major (Bt[n][k] = B[k][n]). So the caller passes Bt = B^T.
// One warp computes the whole GEMM (correctness-first; tiling loops over
// M/16 x N/8 x K/16). Validates against a CPU reference.
#include <cstdio>
#include <cstdint>
#include <vector>
#include <random>
#include <cmath>
#include <cuda_runtime.h>
#include <cuda_bf16.h>

using u16 = uint16_t;

__device__ __forceinline__ void MmaBf16(float& c0, float& c1, float& c2,
                                        float& c3, uint32_t a0, uint32_t a1,
                                        uint32_t a2, uint32_t a3, uint32_t b0,
                                        uint32_t b1) {
  asm volatile(
      "mma.sync.aligned.m16n8k16.row.col.f32.bf16.bf16.f32 "
      "{%0,%1,%2,%3}, {%4,%5,%6,%7}, {%8,%9}, {%0,%1,%2,%3};\n"
      : "+f"(c0), "+f"(c1), "+f"(c2), "+f"(c3)
      : "r"(a0), "r"(a1), "r"(a2), "r"(a3), "r"(b0), "r"(b1));
}

// A: [M,K] row-major bf16. Bt: [N,K] row-major bf16 (== B^T). C: [M,N] f32.
// M multiple of 16, N multiple of 8, K multiple of 16. Single warp.
__global__ void GemmKernel(const u16* A, const u16* Bt, float* C, int M, int N,
                           int K) {
  const int lane = threadIdx.x;  // 0..31
  const int group = lane >> 2;   // 0..7 (M-row within a 16-tile: group, group+8)
  const int k0 = (lane & 3) * 2; // K-pair base
  const int col = (lane & 3) * 2;// N-pair base (C fragment)
  for (int mt = 0; mt < M / 16; ++mt) {
    for (int nt = 0; nt < N / 8; ++nt) {
      float c0 = 0.f, c1 = 0.f, c2 = 0.f, c3 = 0.f;
      for (int kt = 0; kt < K / 16; ++kt) {
        const int mb = mt * 16, nb = nt * 8, kb = kt * 16;
        const u16* a = A + (mb + group) * K + kb + k0;
        const u16* a8 = A + (mb + group + 8) * K + kb + k0;
        uint32_t a0 = *reinterpret_cast<const uint32_t*>(a);
        uint32_t a1 = *reinterpret_cast<const uint32_t*>(a8);
        uint32_t a2 = *reinterpret_cast<const uint32_t*>(a + 8);
        uint32_t a3 = *reinterpret_cast<const uint32_t*>(a8 + 8);
        // B operand: Bt[n][k], n = nb+group, contiguous in k.
        const u16* b = Bt + (nb + group) * K + kb + k0;
        uint32_t b0 = *reinterpret_cast<const uint32_t*>(b);
        uint32_t b1 = *reinterpret_cast<const uint32_t*>(b + 8);
        MmaBf16(c0, c1, c2, c3, a0, a1, a2, a3, b0, b1);
      }
      const int mb = mt * 16, nb = nt * 8;
      C[(mb + group) * N + nb + col] = c0;
      C[(mb + group) * N + nb + col + 1] = c1;
      C[(mb + group + 8) * N + nb + col] = c2;
      C[(mb + group + 8) * N + nb + col + 1] = c3;
    }
  }
}

static u16 f2b(float f) {
  __nv_bfloat16 b = __float2bfloat16_rn(f);
  return *reinterpret_cast<u16*>(&b);
}
static float b2f(u16 h) {
  uint32_t u = (uint32_t)h << 16; float f; memcpy(&f, &u, 4); return f;
}

int main() {
  const int M = 32, N = 128, K = 128;
  std::mt19937 rng(1);
  std::normal_distribution<float> d(0.f, 1.f);
  std::vector<float> Af(M * K), Bf(K * N);
  for (auto& x : Af) x = d(rng);
  for (auto& x : Bf) x = d(rng);
  // Bt[N,K] = B^T
  std::vector<u16> Ah(M * K), Bth(N * K);
  for (int i = 0; i < M * K; ++i) Ah[i] = f2b(Af[i]);
  for (int n = 0; n < N; ++n)
    for (int k = 0; k < K; ++k) Bth[n * K + k] = f2b(Bf[k * N + n]);
  u16 *dA, *dBt; float* dC;
  cudaMalloc(&dA, M * K * 2); cudaMalloc(&dBt, N * K * 2); cudaMalloc(&dC, M * N * 4);
  cudaMemcpy(dA, Ah.data(), M * K * 2, cudaMemcpyHostToDevice);
  cudaMemcpy(dBt, Bth.data(), N * K * 2, cudaMemcpyHostToDevice);
  GemmKernel<<<1, 32>>>(dA, dBt, dC, M, N, K);
  cudaDeviceSynchronize();
  std::vector<float> Cg(M * N);
  cudaMemcpy(Cg.data(), dC, M * N * 4, cudaMemcpyDeviceToHost);
  // CPU ref (bf16-rounded inputs to match)
  double num = 0, den = 0, mx = 0;
  for (int m = 0; m < M; ++m)
    for (int n = 0; n < N; ++n) {
      float acc = 0.f;
      for (int k = 0; k < K; ++k) acc += b2f(Ah[m * K + k]) * b2f(Bth[n * K + k]);
      double diff = Cg[m * N + n] - acc;
      num += diff * diff; den += (double)acc * acc; mx = std::max(mx, std::abs(diff));
    }
  printf("GEMM %dx%dx%d  l2_rel=%.3e  max_abs=%.3e\n", M, N, K,
         std::sqrt(num / den), mx);
  return 0;
}
