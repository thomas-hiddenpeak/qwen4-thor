// Single-token QSA: four disjoint output-column partitions per KV head.
// Repeats QK and softmax, preserves the per-output chunk/MMA order.
#include "q4t/model/qsa_decode.h"

#include <cuda_bf16.h>
#include <cuda_runtime.h>

#include <cstdint>
#include <cstring>

#include "q4t/model/full_attention.h"

namespace q4t::model {
namespace {
using u16 = uint16_t;
using f32 = float;
__device__ __forceinline__ f32 Bf16ToFloat(u16 x) {
  uint32_t bits = static_cast<uint32_t>(x) << 16;
  float f;
  std::memcpy(&f, &bits, sizeof(f));
  return f;
}
__device__ __forceinline__ u16 FloatToBf16(f32 f) {
  const __nv_bfloat16 b = __float2bfloat16_rn(f);
  return *reinterpret_cast<const u16*>(&b);
}

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

// Two alternating K/V tiles; wait for the consumed tile before reading.
__device__ __forceinline__ void CpAsync16(void* smem, const void* gmem) {
  const unsigned s = static_cast<unsigned>(__cvta_generic_to_shared(smem));
  asm volatile("cp.async.cg.shared.global [%0], [%1], 16;\n" ::"r"(s),
               "l"(gmem));
}
__device__ __forceinline__ void CpAsyncCommit() {
  asm volatile("cp.async.commit_group;\n");
}
template <int N>
__device__ __forceinline__ void CpAsyncWait() {
  asm volatile("cp.async.wait_group %0;\n" ::"n"(N));
}

__global__ void SparseAttentionDecodeSplitKernel(
    const u16* __restrict__ q, const u16* __restrict__ kv_cache,
    const int* __restrict__ page_table, const int* __restrict__ topk,
    const int* __restrict__ topk_len, u16* __restrict__ out,
    const u16* __restrict__ gate, int nq, int nkv, int hd, int max_topk,
    const int* __restrict__ d_seq_id, size_t kv_seq_stride, int pt_seq_stride) {
  constexpr int kG = 12;
  constexpr int kHd = 256;
  constexpr int kSharedHd = kHd + 8;
  constexpr int kValueCols = 64;
  constexpr int kSharedVd = kValueCols + 8;
  constexpr int kChunk = 16;
  if (nq / nkv != kG || nkv != 2 || hd != kHd) return;
  __shared__ u16 sQ[16 * kSharedHd];
  __shared__ u16 sK[2 * kChunk * kSharedHd];
  __shared__ u16 sV[2 * kChunk * kSharedVd];
  __shared__ float sS[16 * 16];
  __shared__ u16 sP[kChunk * 16];
  __shared__ float sMax[16];
  __shared__ float sSum[16];
  __shared__ float sAlpha[16];
  const int t = blockIdx.x;
  const int kvh = blockIdx.y;
  const int column_base = blockIdx.z * kValueCols;
  const int warp_id = threadIdx.x >> 5;
  const int lane = threadIdx.x & 31;
  const u16* kv =
      d_seq_id ? kv_cache + static_cast<size_t>(d_seq_id[t]) * kv_seq_stride
               : kv_cache;
  const int* pt =
      d_seq_id ? page_table + static_cast<size_t>(d_seq_id[t]) * pt_seq_stride
               : page_table;
  const int* sel = topk + static_cast<size_t>(t) * max_topk;
  const int nsel_total = topk_len[t];
  const float scale = 1.f / sqrtf(static_cast<float>(kHd));
  const int qh0 = kvh * kG;
  for (int i = threadIdx.x; i < 16 * kHd; i += 256) {
    const int m = i / kHd;
    sQ[m * kSharedHd + i % kHd] =
        (m < kG) ? q[(static_cast<size_t>(t) * nq + qh0 + m) * kHd + (i % kHd)]
                 : 0;
  }
  for (int i = threadIdx.x; i < kChunk * 16; i += 256) sP[i] = 0;
  if (threadIdx.x < 16) {
    sMax[threadIdx.x] = -1e30f;
    sSum[threadIdx.x] = 0.f;
    sAlpha[threadIdx.x] = 0.f;
  }
  __syncthreads();
  const int pv_group = lane >> 2;
  const int pv_col = (lane & 3) * 2;
  // One PV tile per warp; each z partition owns 64 disjoint columns.
  float acc[1][4];
#pragma unroll
  for (int j = 0; j < 1; ++j) {
    acc[j][0] = 0.f;
    acc[j][1] = 0.f;
    acc[j][2] = 0.f;
    acc[j][3] = 0.f;
  }
  auto stage = [&](int base_sel, int chunk, int buf) {
    u16* dK = sK + static_cast<size_t>(buf) * kChunk * kSharedHd;
    u16* dV = sV + static_cast<size_t>(buf) * kChunk * kSharedVd;
    for (int i = threadIdx.x; i < chunk * 32; i += 256) {
      const int c = i / 32;
      const int d8 = (i % 32) * 8;
      const int p = sel[base_sel + c];
      const int sp = p < 0 ? 0 : p;
      const int slot = pt[sp] * kKvPageSize + (sp % kKvPageSize);
      const size_t g = (static_cast<size_t>(slot) * nkv + kvh) * (2 * kHd) + d8;
      CpAsync16(dK + c * kSharedHd + d8, kv + g);
    }
    for (int i = threadIdx.x; i < chunk * 8; i += 256) {
      const int c = i / 8;
      const int d8 = (i % 8) * 8;
      const int p = sel[base_sel + c];
      const int sp = p < 0 ? 0 : p;
      const int slot = pt[sp] * kKvPageSize + (sp % kKvPageSize);
      const size_t g = (static_cast<size_t>(slot) * nkv + kvh) * (2 * kHd) +
                       kHd + column_base + d8;
      CpAsync16(dV + c * kSharedVd + d8, kv + g);
    }
    for (int i = threadIdx.x; i < (kChunk - chunk) * kHd; i += 256) {
      dK[(chunk + i / kHd) * kSharedHd + i % kHd] = 0;
    }
    for (int i = threadIdx.x; i < (kChunk - chunk) * kValueCols; i += 256) {
      dV[(chunk + i / kValueCols) * kSharedVd + i % kValueCols] = 0;
    }
    CpAsyncCommit();
  };
  int nsel = 0;
  int buf = 0;
  if (nsel_total > 0) stage(0, min(kChunk, nsel_total), 0);
  while (nsel < nsel_total) {
    const int chunk = min(kChunk, nsel_total - nsel);
    const int next_nsel = nsel + chunk;
    if (next_nsel < nsel_total) {
      stage(next_nsel, min(kChunk, nsel_total - next_nsel), buf ^ 1);
      CpAsyncWait<1>();
    } else {
      CpAsyncWait<0>();
    }
    __syncthreads();
    const u16* bK = sK + static_cast<size_t>(buf) * kChunk * kSharedHd;
    const u16* bV = sV + static_cast<size_t>(buf) * kChunk * kSharedVd;
    if (warp_id < kChunk / 8) {
      const int nt = warp_id;
      const int nb = nt * 8;
      const int group = lane >> 2;
      const int k0 = (lane & 3) * 2;
      const int col = (lane & 3) * 2;
      float c0 = 0.f, c1 = 0.f, c2 = 0.f, c3 = 0.f;
      for (int kt = 0; kt < kHd / 16; ++kt) {
        const int kb = kt * 16;
        const u16* qa = sQ + group * kSharedHd + kb + k0;
        const u16* qa8 = sQ + (group + 8) * kSharedHd + kb + k0;
        uint32_t a0 = *reinterpret_cast<const uint32_t*>(qa);
        uint32_t a1 = *reinterpret_cast<const uint32_t*>(qa8);
        uint32_t a2 = *reinterpret_cast<const uint32_t*>(qa + 8);
        uint32_t a3 = *reinterpret_cast<const uint32_t*>(qa8 + 8);
        const u16* kbp = bK + (nb + group) * kSharedHd + kb + k0;
        uint32_t b0 = *reinterpret_cast<const uint32_t*>(kbp);
        uint32_t b1 = *reinterpret_cast<const uint32_t*>(kbp + 8);
        MmaBf16(c0, c1, c2, c3, a0, a1, a2, a3, b0, b1);
      }
      sS[group * 16 + nb + col] = c0;
      sS[group * 16 + nb + col + 1] = c1;
      sS[(group + 8) * 16 + nb + col] = c2;
      sS[(group + 8) * 16 + nb + col + 1] = c3;
    }
    __syncthreads();
    if (threadIdx.x < kG) {
      const int qrow = threadIdx.x;
      const float m_old = sMax[qrow];
      float mx = m_old;
      for (int p = 0; p < chunk; ++p) mx = fmaxf(mx, sS[qrow * 16 + p] * scale);
      const float alpha = expf(m_old - mx);
      sAlpha[qrow] = alpha;
      float s = sSum[qrow] * alpha;
      for (int p = 0; p < kChunk; ++p) {
        const float pr =
            (p < chunk) ? expf(sS[qrow * 16 + p] * scale - mx) : 0.f;
        sP[p * 16 + qrow] = FloatToBf16(pr);
        s += pr;
      }
      sSum[qrow] = s;
      sMax[qrow] = mx;
    }
    __syncthreads();
    {
      const int k0 = (lane & 3) * 2;
      const float ag = sAlpha[pv_group];
      const float ag8 = sAlpha[pv_group + 8];
      const u16* pa = sP + pv_group;
      const u16* pa8 = sP + pv_group + 8;
      uint32_t a0 = static_cast<uint32_t>(pa[k0 * 16]) |
                    (static_cast<uint32_t>(pa[(k0 + 1) * 16]) << 16);
      uint32_t a1 = static_cast<uint32_t>(pa8[k0 * 16]) |
                    (static_cast<uint32_t>(pa8[(k0 + 1) * 16]) << 16);
      uint32_t a2 = static_cast<uint32_t>(pa[(k0 + 8) * 16]) |
                    (static_cast<uint32_t>(pa[(k0 + 9) * 16]) << 16);
      uint32_t a3 = static_cast<uint32_t>(pa8[(k0 + 8) * 16]) |
                    (static_cast<uint32_t>(pa8[(k0 + 9) * 16]) << 16);
#pragma unroll
      for (int j = 0; j < 1; ++j) {
        const int nt = warp_id + j * 8;
        const int nb = nt * 8;
        const u16* vb = bV + nb + pv_group;
        uint32_t b0 = static_cast<uint32_t>(vb[k0 * kSharedVd]) |
                      (static_cast<uint32_t>(vb[(k0 + 1) * kSharedVd]) << 16);
        uint32_t b1 = static_cast<uint32_t>(vb[(k0 + 8) * kSharedVd]) |
                      (static_cast<uint32_t>(vb[(k0 + 9) * kSharedVd]) << 16);
        float c0 = 0.f, c1 = 0.f, c2 = 0.f, c3 = 0.f;
        MmaBf16(c0, c1, c2, c3, a0, a1, a2, a3, b0, b1);
        acc[j][0] = acc[j][0] * ag + c0;
        acc[j][1] = acc[j][1] * ag + c1;
        acc[j][2] = acc[j][2] * ag8 + c2;
        acc[j][3] = acc[j][3] * ag8 + c3;
      }
    }
    __syncthreads();
    nsel = next_nsel;
    buf ^= 1;
  }
  {
    const float lg = sSum[pv_group];
    const float lg8 = sSum[pv_group + 8];
    const int qh_a = qh0 + pv_group;
    const int qh_b = qh0 + pv_group + 8;
#pragma unroll
    for (int j = 0; j < 1; ++j) {
      const int nb = (warp_id + j * 8) * 8;
      const int d = column_base + nb + pv_col;
      {
        const size_t o0 = (static_cast<size_t>(t) * nq + qh_a) * kHd + d;
        float v0 = (lg > 0.f) ? acc[j][0] / lg : acc[j][0];
        float v1 = (lg > 0.f) ? acc[j][1] / lg : acc[j][1];
        const float g0 = 1.f / (1.f + expf(-Bf16ToFloat(gate[o0])));
        const float g1 = 1.f / (1.f + expf(-Bf16ToFloat(gate[o0 + 1])));
        out[o0] = FloatToBf16(Bf16ToFloat(FloatToBf16(v0)) * g0);
        out[o0 + 1] = FloatToBf16(Bf16ToFloat(FloatToBf16(v1)) * g1);
      }
      if (pv_group + 8 < kG) {
        const size_t o0 = (static_cast<size_t>(t) * nq + qh_b) * kHd + d;
        float v0 = (lg8 > 0.f) ? acc[j][2] / lg8 : acc[j][2];
        float v1 = (lg8 > 0.f) ? acc[j][3] / lg8 : acc[j][3];
        const float g0 = 1.f / (1.f + expf(-Bf16ToFloat(gate[o0])));
        const float g1 = 1.f / (1.f + expf(-Bf16ToFloat(gate[o0 + 1])));
        out[o0] = FloatToBf16(Bf16ToFloat(FloatToBf16(v0)) * g0);
        out[o0 + 1] = FloatToBf16(Bf16ToFloat(FloatToBf16(v1)) * g1);
      }
    }
  }
}

}  // namespace
Status QsaDecodeSplit(const uint16_t* q, const uint16_t* kv_cache,
                      const int* page_table, const int* topk,
                      const int* topk_len, uint16_t* out, const uint16_t* gate,
                      int max_topk, const int* seq_id, size_t kv_seq_stride,
                      int pt_seq_stride, cudaStream_t stream) {
  SparseAttentionDecodeSplitKernel<<<dim3(1, 2, 4), 256, 0, stream>>>(
      q, kv_cache, page_table, topk, topk_len, out, gate, 24, 2, 256, max_topk,
      seq_id, kv_seq_stride, pt_seq_stride);
  const cudaError_t error = cudaGetLastError();
  if (error != cudaSuccess) return Status::Fail(cudaGetErrorString(error));
  return {};
}
}  // namespace q4t::model
