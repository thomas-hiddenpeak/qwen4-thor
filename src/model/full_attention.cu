// Full attention (QSA sparse attention) — implementation.
//
// See include/q4t/model/full_attention.h for the full math. This file
// implements the 11 kernels + the Bf16Gemm projections.
//
// Key simplification: for a sequence of length L, the number of visible
// compressed blocks is ceil(L / compress_ratio). With budget 2048 and
// compress_ratio 4, block_topk = 512, so any sequence up to 2048 tokens has
// <= 512 visible blocks and the QSA topk selects ALL of them — i.e. QSA
// degenerates to dense causal attention. Sparsity only kicks in beyond
// 2048 tokens. The indexer is still run (to match the reference) but its
// selection is a no-op in the dense regime; the sparse attention kernel
// handles both regimes via the topk index list.
#include "q4t/model/full_attention.h"

#include <cublasLt.h>
#include <cuda_bf16.h>
#include <cuda_runtime.h>

#include <cmath>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <string>
#include <vector>

#include "q4t/io/weight_loader.h"
#include "q4t/model/linear.h"
#include "q4t/status.h"

namespace q4t {
namespace model {
namespace {

constexpr int kMaxT = 8192;  // max prefill tokens handled by the attention
constexpr int kMaxBlocks = 2048;  // max compressed blocks (kMaxT / 4)
constexpr int kMaxTopk = 2052;  // idx_budget + compress - 1

using u16 = uint16_t;
using f32 = float;

// Bit-identity BF16<->FP32 conversions (must NOT go through __nv_bfloat16's
// value constructors, which integer-promote a uint16 to float and corrupt the
// bit pattern). Mirrors linear_attention.cu.
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

Status CheckGemm(const Bf16GemmResult& r) {
  if (r.status != CUBLAS_STATUS_SUCCESS || !r.has_algo) {
    return Status::Fail(std::string("Bf16Gemm failed (status=") +
                        std::to_string(static_cast<int>(r.status)) + ")");
  }
  return Status();
}

// ---------------------------------------------------------------------------
// Kernel 1: deinterleave qg -> q, gate, then per-head centered RMSNorm(q)
//
//   qg : [T, nq, 2*hd]  (per head: [hd q-dims][hd gate-dims])
//   q  : [T, nq, hd]    (centered RMSNorm'd, pre-RoPE)
//   gate: [T, nq, hd]
//
// block = one (t, h), thread = d. centered RMSNorm:
//   y = x * rsqrt(mean(x^2)+eps) * (1 + w[d]).
__global__ void DeinterleaveQNormKernel(const u16* __restrict__ qg,
                                        const u16* __restrict__ q_norm,
                                        u16* __restrict__ q,
                                        u16* __restrict__ gate, int nq, int hd,
                                        float eps) {
  int th = blockIdx.x;  // t * nq + h
  int t = th / nq;
  int h = th % nq;
  int d = threadIdx.x;
  if (d >= hd) return;
  size_t base = (static_cast<size_t>(t) * nq + h) * (2 * hd);
  f32 qv = Bf16ToFloat(qg[base + d]);
  // gate is a raw BF16 bit copy (no conversion).
  gate[static_cast<size_t>(t) * nq * hd + h * hd + d] = qg[base + hd + d];
  __shared__ float head_sum[1];
  if (threadIdx.x == 0) head_sum[0] = 0.f;
  __syncthreads();
  atomicAdd(&head_sum[0], qv * qv);
  __syncthreads();
  float rs = rsqrtf(head_sum[0] / hd + eps);
  f32 w = Bf16ToFloat(q_norm[d]);
  q[static_cast<size_t>(t) * nq * hd + h * hd + d] =
      FloatToBf16(qv * rs * (1.f + w));
}

// ---------------------------------------------------------------------------
// Kernel 2: per-head centered RMSNorm(k)
//
//   k : [T, nkv, hd] (in/out)
//   k_norm: [hd]
// block = one (t, kv), thread = d.
// k_out may alias k (in-place norm).
__global__ void KNormKernel(const u16* __restrict__ k, const u16* __restrict__ k_norm,
                            u16* k_out, int T, int nkv, int hd,
                            float eps) {
  int t = blockIdx.x / nkv;
  int h = blockIdx.x % nkv;
  int d = threadIdx.x;
  if (d >= hd) return;
  size_t base = (static_cast<size_t>(t) * nkv + h) * hd;
  f32 kv = Bf16ToFloat(k[base + d]);
  __shared__ float head_sum[1];
  if (threadIdx.x == 0) head_sum[0] = 0.f;
  __syncthreads();
  atomicAdd(&head_sum[0], kv * kv);
  __syncthreads();
  float rs = rsqrtf(head_sum[0] / hd + eps);
  f32 w = Bf16ToFloat(k_norm[d]);
  k_out[base + d] = FloatToBf16(kv * rs * (1.f + w));
}

// ---------------------------------------------------------------------------
// Kernel 3: partial MRoPE (RoPE) on the first rot_d dims of q and k.
//
//   q : [T, nq, hd]  (in/out, first rot_d dims rotated)
//   k : [T, nkv, hd] (in/out)
//   positions: [T] int32
//
// RoPE: for i in [0, rot_d/2):
//   freq_i = theta^(-2i/rot_d)
//   q[i]   = q[i]*cos + q[i+half]*sin
//   q[i+half] = -q[i]*sin + q[i+half]*cos
// One thread per (t, h, i) for i in [0, half).
template <int N>
__global__ void PartialRopeKernel(u16* __restrict__ x, int n_heads, int hd,
                                  int rot_d, const int* __restrict__ positions,
                                  float theta, int T) {
  int half = rot_d / 2;
  int idx = blockIdx.x * blockDim.x + threadIdx.x;
  int total = T * N * half;
  if (idx >= total) return;
  int i = idx % half;
  int h = (idx / half) % N;
  int t = idx / (half * N);
  float pos = static_cast<float>(positions[t]);
  float inv_freq = powf(theta, -2.f * i / rot_d);
  float ang = pos * inv_freq;
  float c = cosf(ang), s = sinf(ang);
  size_t base = (static_cast<size_t>(t) * N + h) * hd + i;
  f32 a = Bf16ToFloat(x[base]);
  f32 b = Bf16ToFloat(x[base + half]);
  x[base] = FloatToBf16(a * c + b * s);
  x[base + half] = FloatToBf16(-a * s + b * c);
}

// ---------------------------------------------------------------------------
// Kernel 4: write k, v into the paged KV cache (interleaved per position).
//
//   k : [T, nkv, hd], v : [T, nkv, hd]
//   kv_cache: [n_pages, kKvPageSize, nkv, 2, hd]
//   page_table: [max_len] — page_table[pos] = physical page for logical pos
//   positions: [T]
// Logical position p lands in physical slot
//   page_table[p] * kKvPageSize + (p % kKvPageSize).
// With the identity page table (page_table[p] = p / kKvPageSize) this is
// bit-identical to the legacy contiguous layout.
__global__ void WriteKVKernel(const u16* __restrict__ k,
                              const u16* __restrict__ v,
                              u16* __restrict__ kv_cache, int nkv, int hd,
                              const int* __restrict__ positions,
                              const int* __restrict__ page_table, int T) {
  int idx = blockIdx.x * blockDim.x + threadIdx.x;
  int total = T * nkv * hd;
  if (idx >= total) return;
  int d = idx % hd;
  int h = (idx / hd) % nkv;
  int t = idx / (hd * nkv);
  int pos = positions[t];
  size_t src = (static_cast<size_t>(t) * nkv + h) * hd + d;
  int slot = page_table[pos] * kKvPageSize + (pos % kKvPageSize);
  size_t dst =
      (static_cast<size_t>(slot) * nkv + h) * (2 * hd);
  kv_cache[dst + d] = k[src];
  kv_cache[dst + hd + d] = v[src];
}

// ---------------------------------------------------------------------------
// Kernel 5: QSA indexer — GemmaRMSNorm (plain) on iq, ik, then partial RoPE.
//
//   iq : [T, idx_n_heads, idx_head_dim]  (in/out)
//   ik : [T, idx_kv_heads, idx_head_dim] (in/out)
//   q_norm, k_norm: [idx_head_dim] (plain)
//   positions: [T]
//
// Plain RMSNorm: y = x * rsqrt(mean(x^2)+eps) * w[d]  (no +1).
// block = one (t, head), thread = d.
__global__ void IndexerNormRopeKernel(u16* __restrict__ iq,
                                      const u16* __restrict__ iq_norm,
                                      u16* __restrict__ ik,
                                      const u16* __restrict__ ik_norm,
                                      const int* __restrict__ positions,
                                      int T, int n_iq, int n_ik, int hd,
                                      int rot_d, float theta, float eps) {
  // Handle iq and ik in the same kernel: blockIdx.x encodes (t, which, head).
  int total_heads = n_iq + n_ik;
  int t = blockIdx.x / total_heads;
  int which_head = blockIdx.x % total_heads;
  int d = threadIdx.x;
  u16* x;
  const u16* nw;
  if (which_head < n_iq) {
    x = iq + (static_cast<size_t>(t) * n_iq + which_head) * hd;
    nw = iq_norm;
  } else {
    int h = which_head - n_iq;
    x = ik + (static_cast<size_t>(t) * n_ik + h) * hd;
    nw = ik_norm;
  }
  if (d >= hd) return;
  f32 xv = Bf16ToFloat(x[d]);
  __shared__ float head_sum[1];
  if (threadIdx.x == 0) head_sum[0] = 0.f;
  __syncthreads();
  atomicAdd(&head_sum[0], xv * xv);
  __syncthreads();
  float rs = rsqrtf(head_sum[0] / hd + eps);
  f32 w = Bf16ToFloat(nw[d]);
  x[d] = FloatToBf16(xv * rs * w);
  // partial RoPE on first rot_d dims
  int half = rot_d / 2;
  if (d < half) {
    float pos = static_cast<float>(positions[t]);
    float inv_freq = powf(theta, -2.f * d / rot_d);
    float ang = pos * inv_freq;
    float c = cosf(ang), s = sinf(ang);
    f32 a = Bf16ToFloat(x[d]);
    f32 b = Bf16ToFloat(x[d + half]);
    x[d] = FloatToBf16(a * c + b * s);
    x[d + half] = FloatToBf16(-a * s + b * c);
  }
}

// ---------------------------------------------------------------------------
// Kernel 6: store raw index keys (pre-RoPE) per token + build compressed keys.
//
// The raw ik (pre-RoPE, post-norm) is stored per token in idx_raw. When a
// group of `compress` tokens completes, the group's average (FP32) is
// GemmaRMSNorm'd and RoPE'd at the group's first position -> idx_comp.
//
// This kernel is launched once per token position t (grid = T). For each t:
//   - copy ik[t] (pre-RoPE) into idx_raw[pos]
//   - if (t+1) % compress == 0: compute group avg over the last `compress`
//     raw tokens, norm+rope, write idx_comp[group_idx]
//
//   ik_raw : [T, idx_head_dim] (pre-RoPE index keys, from the projection)
//   ik_norm: [idx_head_dim]
//   idx_raw: [max_len, idx_head_dim]
//   idx_comp: [max_len, idx_head_dim]
//   positions: [T]
// Split of the former single BuildCompressedKKernel. The raw store and the
// compressed build are separate kernels so the kernel-boundary global
// sync guarantees every idx_raw write of the batch is visible before any
// compressed build reads its group's raw tokens. A single fused kernel had
// a prefill race: the group-tail block (t=3) could read idx_raw[0..2]
// before blocks t=0..2 wrote them, silently corrupting idx_comp (harmless
// for the dense prefill, but those keys persist and are used by the sparse
// indexer once decode crosses indexer_budget).
__global__ void WriteIndexRawKernel(const u16* __restrict__ ik_raw,
                                    u16* __restrict__ idx_raw,
                                    const int* __restrict__ positions, int T,
                                    int hd) {
  int t = blockIdx.x;
  if (t >= T) return;
  int pos = positions[t];
  int d = threadIdx.x;
  if (d >= hd) return;
  idx_raw[static_cast<size_t>(pos) * hd + d] = ik_raw[static_cast<size_t>(t) * hd + d];
}

__global__ void BuildCompressedKKernel(const u16* __restrict__ ik_norm,
                                       const u16* __restrict__ idx_raw,
                                       u16* __restrict__ idx_comp,
                                       const int* __restrict__ positions, int T,
                                       int hd, int compress, float theta,
                                       float eps) {
  int t = blockIdx.x;
  if (t >= T) return;
  int pos = positions[t];
  int d = threadIdx.x;
  if (d >= hd) return;
  // group completion: position `pos` is the last of its group when
  // (pos+1) % compress == 0. Must use `pos`, not the batch index `t`:
  // during decode T=1 and t=0 always, so (t+1)%compress never fires and
  // the compressed key is never built. The group index is pos/compress.
  if ((pos + 1) % compress != 0) return;
  int group = pos / compress;  // group index (0-based)
  int g0 = group * compress;  // first position in the group
  // average over the `compress` raw tokens in this group (FP32). All of
  // these raw writes happened in earlier decode steps or in the preceding
  // WriteIndexRawKernel launch, so they are visible here.
  float acc = 0.f;
  for (int j = 0; j < compress; ++j) {
    acc += Bf16ToFloat(idx_raw[static_cast<size_t>(g0 + j) * hd + d]);
  }
  acc /= compress;
  // GemmaRMSNorm (plain) over the head: need sum of squares
  __shared__ float head_sum[1];
  if (threadIdx.x == 0) head_sum[0] = 0.f;
  __syncthreads();
  atomicAdd(&head_sum[0], acc * acc);
  __syncthreads();
  float rs = rsqrtf(head_sum[0] / hd + eps);
  f32 w = Bf16ToFloat(ik_norm[d]);
  float normed = acc * rs * w;
  // partial RoPE at the group's first position. Positions are the
  // contiguous 0..N-1, so the group's first position IS g0. (Reading
  // positions[g0] would be out of bounds during decode, where the
  // positions array has a single element.)
  int half = 64 / 2;  // rot_d = 64 for the indexer (idx_head_dim 128, factor .5)
  float pos0 = static_cast<float>(g0);
  float val;
  if (d < half) {
    float inv_freq = powf(theta, -2.f * d / 64.f);
    float ang = pos0 * inv_freq;
    float c = cosf(ang), s = sinf(ang);
    // need the partner (d+half) normed value; recompute it
    float acc2 = 0.f;
    for (int j = 0; j < compress; ++j) {
      acc2 += Bf16ToFloat(idx_raw[static_cast<size_t>(g0 + j) * hd + (d + half)]);
    }
    acc2 /= compress;
    float w2 = Bf16ToFloat(ik_norm[d + half]);
    float normed2 = acc2 * rs * w2;
    val = normed * c + normed2 * s;
  } else if (d >= half && d < 64) {
    int d2 = d - half;
    float inv_freq = powf(theta, -2.f * d2 / 64.f);
    float ang = pos0 * inv_freq;
    float c = cosf(ang), s = sinf(ang);
    float acc2 = 0.f;
    for (int j = 0; j < compress; ++j) {
      acc2 += Bf16ToFloat(idx_raw[static_cast<size_t>(g0 + j) * hd + d2]);
    }
    acc2 /= compress;
    float w2 = Bf16ToFloat(ik_norm[d2]);
    float normed2 = acc2 * rs * w2;
    val = -normed2 * s + normed * c;
  } else {
    val = normed;  // dims >= 64 unchanged
  }
  idx_comp[static_cast<size_t>(group) * hd + d] = FloatToBf16(val);
}

// ---------------------------------------------------------------------------
// Kernel 7: QSA indexer logits.
//
//   logits[t, g] = (1/sqrt(idx_head_dim)) * sum_h relu(iq[t,h] . ck[g])
//   over visible groups g < (pos+1)/compress.
//
//   iq : [T, n_iq, hd] (RoPE'd)
//   ck : idx_comp [n_groups, hd]
//   logits: [T, max_blocks] FP32 (out)
//   positions: [T]
// block = one token t, 256 threads.
__global__ void IndexerLogitsKernel(const u16* __restrict__ iq,
                                    const u16* __restrict__ ck,
                                    f32* __restrict__ logits,
                                    const int* __restrict__ positions, int T,
                                    int n_iq, int hd, int compress,
                                    int max_blocks) {
  int t = blockIdx.x;
  if (t >= T) return;
  int pos = positions[t];
  int n_groups = (pos + 1) / compress;  // visible groups
  if (n_groups > max_blocks) n_groups = max_blocks;
  float inv_sqrt = 1.f / sqrtf(static_cast<float>(hd));
  // Must hold up to max_blocks (kMaxBlocks) entries: n_groups reaches
  // max_blocks once position >= max_blocks * compress (2048 * 4 = 8192).
  // A smaller buffer (e.g. 512) overflows at position 2051 (n_groups 513),
  // corrupting shared memory -> illegal memory access.
  __shared__ float s_blk[kMaxBlocks];
  for (int g = threadIdx.x; g < n_groups; g += blockDim.x) {
    float sum = 0.f;
    for (int h = 0; h < n_iq; ++h) {
      float dot = 0.f;
      for (int d = 0; d < hd; ++d) {
        dot += Bf16ToFloat(iq[(static_cast<size_t>(t) * n_iq + h) * hd + d]) *
               Bf16ToFloat(ck[static_cast<size_t>(g) * hd + d]);
      }
      sum += fmaxf(dot, 0.f);
    }
    s_blk[g] = sum * inv_sqrt;
  }
  __syncthreads();
  for (int g = threadIdx.x; g < max_blocks; g += blockDim.x) {
    logits[static_cast<size_t>(t) * max_blocks + g] =
        (g < n_groups) ? s_blk[g] : -1e30f;
  }
}

// ---------------------------------------------------------------------------
// Kernel 8: topk block selection -> token index list.
//
// Dense regime (n_visible_blocks <= block_topk): all visible positions are
// selected (QSA == dense causal). Sparse regime: top-`block_topk` blocks by
// logit, expanded to tokens, + the current group's tail tokens.
//
//   topk : [T, max_topk] int32 (out) — selected token positions
//   logits: [T, max_blocks] FP32
//   positions: [T]
// block = one token t, 256 threads.
__global__ void TopkSelectKernel(f32* __restrict__ logits, int* __restrict__ topk,
                                 const int* __restrict__ positions, int T,
                                 int compress, int block_topk, int max_blocks,
                                 int max_topk) {
  __shared__ int s_sel[2048];
  int t = blockIdx.x;
  if (t >= T) return;
  // Selection is inherently sequential; run it on a single thread to avoid
  // shared-memory races (the dense path is a simple 0..pos fill).
  if (threadIdx.x != 0) return;
  int pos = positions[t];
  int n_groups = (pos + 1) / compress;
  if (n_groups > max_blocks) n_groups = max_blocks;
  int* out = topk + static_cast<size_t>(t) * max_topk;
  int n = 0;
  if (n_groups <= block_topk) {
    // dense: all visible positions (QSA == dense causal attention)
    for (int p = 0; p <= pos && n < max_topk; ++p) out[n++] = p;
  } else {
    // sparse: top-`block_topk` blocks by logit, expanded to tokens.
    for (int g = 0; g < n_groups; ++g) s_sel[g] = 0;
    for (int c = 0; c < block_topk; ++c) {
      int best = -1;
      float bv = -1e30f;
      for (int g = 0; g < n_groups; ++g) {
        if (s_sel[g]) continue;
        float v = logits[static_cast<size_t>(t) * max_blocks + g];
        if (v > bv) {
          bv = v;
          best = g;
        }
      }
      if (best < 0) break;
      s_sel[best] = 1;
      for (int j = 0; j < compress && n < max_topk; ++j) {
        int p = best * compress + j;
        if (p <= pos) out[n++] = p;
      }
    }
    // Causality: the current token's group is NOT among the visible
    // compressed keys for 3 of 4 phase values (n_groups = (pos+1)/compress
    // excludes the in-progress group unless pos is a group tail), so the
    // top-`block_topk` selection above never covers the current local
    // context. Force-include the current group's emitted tail
    // [g0_cur, pos] so the query always attends to its own recent tokens.
    int cur_g = pos / compress;
    bool cur_selected = (cur_g < n_groups) && s_sel[cur_g];
    if (!cur_selected) {
      int g0_cur = cur_g * compress;
      for (int p = g0_cur; p <= pos && n < max_topk; ++p) out[n++] = p;
    }
  }
  for (; n < max_topk; ++n) out[n] = -1;
}

// ---------------------------------------------------------------------------
// Kernel 9: sparse GQA attention over the selected token positions.
//
//   q : [T, nq, hd] (RoPE'd), gate applied later
//   kv_cache: [n_pages, kKvPageSize, nkv, 2, hd] (paged)
//   page_table: [max_len] — page_table[pos] = physical page for logical pos
//   topk : [T, max_topk] int32
//   out  : [T, nq, hd] (BF16, pre-gate)
//
// block = one (t, qh), 256 threads (hd). The query row is staged in shared
// memory; selected K/V positions are processed in chunks of 16, staged in
// shared memory, with online (running max/sum) softmax. Each selected logical
// position p is read from physical slot page_table[p]*kKvPageSize + p%kKvPageSize.
// Optimized: each thread owns dim d. Dot products computed via per-dim
// partial (1 FMA) + warp reduce (5 shfl) + cross-warp shared (8 adds),
// eliminating the 256x redundant full-dot computation of the old version.
__global__ void SparseAttentionKernel(const u16* __restrict__ q,
                                      const u16* __restrict__ kv_cache,
                                      const int* __restrict__ page_table,
                                      const int* __restrict__ topk,
                                      u16* __restrict__ out, int nq, int nkv,
                                      int hd, int max_topk) {
  const int CHUNK = 16;
  const int NWARP = 8;  // 256 threads / 32
  __shared__ float sQ[256];
  __shared__ float sK[CHUNK * 256];
  __shared__ float sV[CHUNK * 256];
  __shared__ float s_partial[NWARP * CHUNK];  // warp-reduced partial dots
  __shared__ float s_dot[CHUNK];              // final dot products
  int th = blockIdx.x;
  int t = th / nq;
  int qh = th % nq;
  int kvh = qh / (nq / nkv);
  int d = threadIdx.x;
  int warp_id = d >> 5;
  int lane = d & 31;
  // Load the query row into shared memory (all threads).
  if (d < hd) sQ[d] = Bf16ToFloat(q[(static_cast<size_t>(t) * nq + qh) * hd + d]);
  __syncthreads();
  const int* sel = topk + static_cast<size_t>(t) * max_topk;
  const float scale = 1.f / sqrtf(static_cast<float>(hd));
  float m = -1e30f;
  float l = 0.f;
  float acc = 0.f;
  int nsel = 0;
  while (nsel < max_topk) {
    int chunk = min(CHUNK, max_topk - nsel);
    // Stage K, V for this chunk into shared memory (coalesced: consecutive
    // threads read consecutive dims).
    for (int c = 0; c < chunk; ++c) {
      int p = sel[nsel + c];
      if (p >= 0 && d < hd) {
        int slot = page_table[p] * kKvPageSize + (p % kKvPageSize);
        size_t base = (static_cast<size_t>(slot) * nkv + kvh) * (2 * hd);
        sK[c * 256 + d] = Bf16ToFloat(kv_cache[base + d]);
        sV[c * 256 + d] = Bf16ToFloat(kv_cache[base + hd + d]);
      }
    }
    __syncthreads();
    // Compute dot products: each thread does 1 FMA per position for its dim,
    // then warp-reduce + cross-warp sum.
    float partial[CHUNK];
    #pragma unroll
    for (int c = 0; c < CHUNK; ++c) {
      partial[c] = (c < chunk && sel[nsel + c] >= 0)
                       ? sQ[d] * sK[c * 256 + d]
                       : 0.f;
    }
    // Warp reduce for each position (5 shfl each).
    #pragma unroll
    for (int c = 0; c < CHUNK; ++c) {
      #pragma unroll
      for (int off = 16; off > 0; off >>= 1)
        partial[c] += __shfl_xor_sync(0xffffffffu, partial[c], off);
    }
    // Lane 0 of each warp writes to shared.
    if (lane == 0) {
      #pragma unroll
      for (int c = 0; c < chunk; ++c) s_partial[warp_id * CHUNK + c] = partial[c];
    }
    __syncthreads();
    // Sum across warps (only needed by one thread per position, but all
    // threads need the result for V accumulation — use thread 0..15).
    if (d < chunk) {
      float s = 0.f;
      #pragma unroll
      for (int w = 0; w < NWARP; ++w) s += s_partial[w * CHUNK + d];
      s_dot[d] = s;
    }
    __syncthreads();
    // Online softmax + V accumulation (each thread owns dim d).
    float m_new = m;
    #pragma unroll
    for (int c = 0; c < chunk; ++c) {
      if (sel[nsel + c] >= 0) m_new = fmaxf(m_new, s_dot[c] * scale);
    }
    float alpha = expf(m - m_new);
    float l_new = l * alpha;
    acc *= alpha;
    #pragma unroll
    for (int c = 0; c < chunk; ++c) {
      if (sel[nsel + c] >= 0) {
        float w = expf(s_dot[c] * scale - m_new);
        l_new += w;
        acc += w * sV[c * 256 + d];
      }
    }
    m = m_new;
    l = l_new;
    nsel += chunk;
    __syncthreads();
  }
  if (l > 0.f) acc /= l;
  if (d < hd) out[(static_cast<size_t>(t) * nq + qh) * hd + d] = FloatToBf16(acc);
}

// ---------------------------------------------------------------------------
// Kernel 10: attn *= sigmoid(gate) (in-place).
__global__ void GateMulKernel(u16* __restrict__ attn, const u16* __restrict__ gate,
                              int total) {
  int idx = blockIdx.x * blockDim.x + threadIdx.x;
  if (idx >= total) return;
  attn[idx] = FloatToBf16(Bf16ToFloat(attn[idx]) *
                          (1.f / (1.f + expf(-Bf16ToFloat(gate[idx])))));
}

}  // namespace

void FullAttentionWeights::Free() {
  auto freep = [](void* p) { if (p) cudaFree(p); };
  freep(q_proj); freep(k_proj); freep(v_proj); freep(o_proj);
  freep(q_norm); freep(k_norm);
  freep(index_qk_proj); freep(index_q_norm); freep(index_k_norm);
  q_proj = k_proj = v_proj = o_proj = nullptr;
  q_norm = k_norm = nullptr;
  index_qk_proj = index_q_norm = index_k_norm = nullptr;
}

Status LoadFullAttention(const io::WeightLoader& loader,
                         const std::string& prefix, int hidden_size, int nq,
                         int nkv, int hd, int rot_d, float rope_theta,
                         float eps, int idx_n_heads, int idx_kv_heads,
                         int idx_head_dim, int idx_budget, int idx_compress,
                         FullAttentionWeights* out, cudaStream_t stream) {
  out->hidden_size = hidden_size;
  out->nq = nq;
  out->nkv = nkv;
  out->hd = hd;
  out->rot_d = rot_d;
  out->rope_theta = rope_theta;
  out->eps = eps;
  out->idx_n_heads = idx_n_heads;
  out->idx_kv_heads = idx_kv_heads;
  out->idx_head_dim = idx_head_dim;
  out->idx_budget = idx_budget;
  out->idx_compress = idx_compress;

  auto alloc = [](uint16_t** p, size_t bytes) -> Status {
    if (cudaMalloc(reinterpret_cast<void**>(p), bytes) != cudaSuccess)
      return Status::Fail("cudaMalloc failed");
    return Status();
  };
  auto load = [&loader, stream](const std::string& name, uint16_t* dst,
                                size_t bytes) -> Status {
    std::vector<uint16_t> host(bytes / sizeof(uint16_t));
    Status s = loader.ReadTensor(name, host.data());
    if (!s.ok()) return s;
    if (cudaMemcpyAsync(dst, host.data(), bytes, cudaMemcpyHostToDevice,
                        stream) != cudaSuccess)
      return Status::Fail("H2D failed");
    return Status();
  };

  Status s;
  if (!(s = alloc(&out->q_proj,
                  static_cast<size_t>(nq * 2 * hd) * hidden_size * 2)))
    return s;
  if (!(s = alloc(&out->k_proj,
                  static_cast<size_t>(nkv * hd) * hidden_size * 2)))
    return s;
  if (!(s = alloc(&out->v_proj,
                  static_cast<size_t>(nkv * hd) * hidden_size * 2)))
    return s;
  if (!(s = alloc(&out->o_proj,
                  static_cast<size_t>(hidden_size) * nq * hd * 2)))
    return s;
  if (!(s = alloc(&out->q_norm, static_cast<size_t>(hd) * 2))) return s;
  if (!(s = alloc(&out->k_norm, static_cast<size_t>(hd) * 2))) return s;
  if (!(s = alloc(&out->index_qk_proj,
                  static_cast<size_t>((idx_n_heads + idx_kv_heads) * idx_head_dim) *
                      hidden_size * 2)))
    return s;
  if (!(s = alloc(&out->index_q_norm, static_cast<size_t>(idx_head_dim) * 2)))
    return s;
  if (!(s = alloc(&out->index_k_norm, static_cast<size_t>(idx_head_dim) * 2)))
    return s;

  if (!(s = load(prefix + ".q_proj.weight", out->q_proj,
                 static_cast<size_t>(nq * 2 * hd) * hidden_size * 2)))
    return s;
  if (!(s = load(prefix + ".k_proj.weight", out->k_proj,
                 static_cast<size_t>(nkv * hd) * hidden_size * 2)))
    return s;
  if (!(s = load(prefix + ".v_proj.weight", out->v_proj,
                 static_cast<size_t>(nkv * hd) * hidden_size * 2)))
    return s;
  if (!(s = load(prefix + ".o_proj.weight", out->o_proj,
                 static_cast<size_t>(hidden_size) * nq * hd * 2)))
    return s;
  if (!(s = load(prefix + ".q_norm.weight", out->q_norm,
                 static_cast<size_t>(hd) * 2)))
    return s;
  if (!(s = load(prefix + ".k_norm.weight", out->k_norm,
                 static_cast<size_t>(hd) * 2)))
    return s;
  if (!(s = load(prefix + ".indexer.index_qk_proj.weight", out->index_qk_proj,
                 static_cast<size_t>((idx_n_heads + idx_kv_heads) * idx_head_dim) *
                     hidden_size * 2)))
    return s;
  if (!(s = load(prefix + ".indexer.q_layernorm.weight", out->index_q_norm,
                 static_cast<size_t>(idx_head_dim) * 2)))
    return s;
  if (!(s = load(prefix + ".indexer.k_layernorm.weight", out->index_k_norm,
                 static_cast<size_t>(idx_head_dim) * 2)))
    return s;
  return Status();
}

size_t FullAttentionWorkspaceBytes(const FullAttentionWeights& w, int T) {
  // Must mirror the carve in FullAttentionForward exactly (256-byte aligned
  // regions + the 32 MiB GEMM scratch).
  const int nq = w.nq, nkv = w.nkv, hd = w.hd;
  const int qg_dim = nq * 2 * hd;
  const int kv_dim = nkv * hd;
  const int idx_hd = w.idx_head_dim;
  const int n_iq = w.idx_n_heads, n_ik = w.idx_kv_heads;
  const int max_blocks = 2048;  // kMaxT / idx_compress
  const int max_topk = 2052;    // idx_budget + idx_compress - 1
  size_t off = 0;
  auto alloc = [&](size_t bytes) { off += (bytes + 255) & ~size_t(255); };
  alloc(static_cast<size_t>(T) * qg_dim * 2);  // d_qg
  alloc(static_cast<size_t>(T) * kv_dim * 2);  // d_k
  alloc(static_cast<size_t>(T) * kv_dim * 2);  // d_v
  alloc(static_cast<size_t>(T) * nq * hd * 2);  // d_q
  alloc(static_cast<size_t>(T) * nq * hd * 2);  // d_gate
  alloc(static_cast<size_t>(T) * n_iq * idx_hd * 2);  // d_iq
  alloc(static_cast<size_t>(T) * n_ik * idx_hd * 2);  // d_ik
  alloc(static_cast<size_t>(T) * idx_hd * 2);         // d_ik_raw
  alloc(static_cast<size_t>(T) * max_blocks * 4);     // d_logits (f32)
  alloc(static_cast<size_t>(T) * max_topk * 4);       // d_topk (i32)
  alloc(static_cast<size_t>(T) * nq * hd * 2);        // d_attn
  off += 32u * 1024u * 1024u;  // GEMM scratch
  return off;
}

Status FullAttentionForward(const FullAttentionWeights& w, const uint16_t* x,
                            uint16_t* out, const int* positions,
                            uint16_t* kv_cache, const int* page_table,
                            uint16_t* idx_raw, uint16_t* idx_comp, int T,
                            void* workspace, size_t workspace_bytes,
                            cudaStream_t stream) {
  if (T <= 0 || T > kMaxT)
    return Status::Fail("FullAttentionForward: T out of range [1, " +
                        std::to_string(kMaxT) + "]");
  const int hs = w.hidden_size;
  const int nq = w.nq, nkv = w.nkv, hd = w.hd;
  const int qg_dim = nq * 2 * hd;
  const int kv_dim = nkv * hd;
  const int idx_hd = w.idx_head_dim;
  const int n_iq = w.idx_n_heads, n_ik = w.idx_kv_heads;
  const int max_blocks = kMaxBlocks;
  const int max_topk = kMaxTopk;

  // Workspace layout (BF16 unless noted).
  size_t off = 0;
  auto alloc = [&](size_t bytes) {
    void* p = static_cast<char*>(workspace) + off;
    off += (bytes + 255) & ~size_t(255);
    return p;
  };
  u16* d_qg = static_cast<u16*>(alloc(static_cast<size_t>(T) * qg_dim * 2));
  u16* d_k = static_cast<u16*>(alloc(static_cast<size_t>(T) * kv_dim * 2));
  u16* d_v = static_cast<u16*>(alloc(static_cast<size_t>(T) * kv_dim * 2));
  u16* d_q = static_cast<u16*>(alloc(static_cast<size_t>(T) * nq * hd * 2));
  u16* d_gate = static_cast<u16*>(alloc(static_cast<size_t>(T) * nq * hd * 2));
  u16* d_iq = static_cast<u16*>(alloc(static_cast<size_t>(T) * n_iq * idx_hd * 2));
  u16* d_ik = static_cast<u16*>(alloc(static_cast<size_t>(T) * n_ik * idx_hd * 2));
  u16* d_ik_raw = static_cast<u16*>(alloc(static_cast<size_t>(T) * idx_hd * 2));
  f32* d_logits = static_cast<f32*>(alloc(static_cast<size_t>(T) * max_blocks * 4));
  int* d_topk = static_cast<int*>(alloc(static_cast<size_t>(T) * max_topk * 4));
  u16* d_attn = static_cast<u16*>(alloc(static_cast<size_t>(T) * nq * hd * 2));
  if (off > workspace_bytes)
    return Status::Fail("FullAttentionForward: workspace too small (need " +
                        std::to_string(off) + ", have " +
                            std::to_string(workspace_bytes) + ")");

  const size_t gemm_ws = 32u * 1024u * 1024u;
  void* d_gemm_ws = static_cast<char*>(workspace) + off;
  off += gemm_ws;
  if (off > workspace_bytes)
    return Status::Fail("FullAttentionForward: workspace too small for GEMM");

  int* d_positions = nullptr;
  if (cudaMallocAsync(&d_positions, T * 4, stream) != cudaSuccess)
    return Status::Fail("cudaMallocAsync positions failed");
  if (cudaMemcpyAsync(d_positions, positions, T * 4, cudaMemcpyHostToDevice,
                      stream) != cudaSuccess) {
    cudaFreeAsync(d_positions, stream);
    return Status::Fail("cudaMemcpyAsync positions failed");
  }

  Status s;
  // 1. qg = x @ W_q^T ; k = x @ W_k^T ; v = x @ W_v^T
  s = CheckGemm(Bf16Gemm(x, w.q_proj, d_qg, T, qg_dim, hs, 1.f, 0.f, d_gemm_ws,
                         gemm_ws, stream));
  if (!s.ok()) return s;
  s = CheckGemm(Bf16Gemm(x, w.k_proj, d_k, T, kv_dim, hs, 1.f, 0.f, d_gemm_ws,
                         gemm_ws, stream));
  if (!s.ok()) return s;
  s = CheckGemm(Bf16Gemm(x, w.v_proj, d_v, T, kv_dim, hs, 1.f, 0.f, d_gemm_ws,
                         gemm_ws, stream));
  if (!s.ok()) return s;

  // 2. deinterleave qg -> q, gate + centered RMSNorm(q)
  DeinterleaveQNormKernel<<<T * nq, hd, 0, stream>>>(d_qg, w.q_norm, d_q,
                                                     d_gate, nq, hd, w.eps);
  // 3. centered RMSNorm(k) (in place)
  KNormKernel<<<T * nkv, hd, 0, stream>>>(d_k, w.k_norm, d_k, T, nkv, hd, w.eps);
  // 4. partial RoPE on q (nq heads) and k (nkv heads), first rot_d dims
  const int half = w.rot_d / 2;
  PartialRopeKernel<24><<<(T * nq * half + 255) / 256, 256, 0, stream>>>(
      d_q, nq, hd, w.rot_d, d_positions, w.rope_theta, T);
  PartialRopeKernel<2><<<(T * nkv * half + 255) / 256, 256, 0, stream>>>(
      d_k, nkv, hd, w.rot_d, d_positions, w.rope_theta, T);
  // 5. write k, v into the paged KV cache
  WriteKVKernel<<<(T * nkv * hd + 255) / 256, 256, 0, stream>>>(
      d_k, d_v, kv_cache, nkv, hd, d_positions, page_table, T);
  // 6. indexer projections: iq = x @ W_iq^T [T,512], ik = x @ W_ik^T [T,128]
  const int iq_dim = n_iq * idx_hd;
  const int ik_dim = n_ik * idx_hd;
  s = CheckGemm(Bf16Gemm(x, w.index_qk_proj, d_iq, T, iq_dim, hs, 1.f, 0.f,
                         d_gemm_ws, gemm_ws, stream));
  if (!s.ok()) return s;
  s = CheckGemm(Bf16Gemm(x, w.index_qk_proj + static_cast<size_t>(iq_dim) * hs,
                         d_ik, T, ik_dim, hs, 1.f, 0.f, d_gemm_ws, gemm_ws,
                         stream));
  if (!s.ok()) return s;
  // ik_raw = pre-norm ik (for compressed-key construction)
  if (cudaMemcpyAsync(d_ik_raw, d_ik, static_cast<size_t>(T) * ik_dim * 2,
                      cudaMemcpyDeviceToDevice, stream) != cudaSuccess)
    return Status::Fail("cudaMemcpyAsync ik_raw failed");
  // 7. indexer GemmaRMSNorm (plain) + partial RoPE on iq, ik (in place)
  IndexerNormRopeKernel<<<T * (n_iq + n_ik), idx_hd, 0, stream>>>(
      d_iq, w.index_q_norm, d_ik, w.index_k_norm, d_positions, T, n_iq, n_ik,
      idx_hd, w.rot_d, w.rope_theta, w.eps);
  // 8a. store raw index keys (per token).
  WriteIndexRawKernel<<<T, idx_hd, 0, stream>>>(d_ik_raw, idx_raw, d_positions,
                                                T, idx_hd);
  // 8b. build compressed keys for groups completed in this batch. Runs in a
  // separate launch so the kernel-boundary sync makes every idx_raw write
  // from 8a visible before the group averages read them (no prefill race).
  BuildCompressedKKernel<<<T, idx_hd, 0, stream>>>(
      w.index_k_norm, idx_raw, idx_comp, d_positions, T, idx_hd,
      w.idx_compress, w.rope_theta, w.eps);
  // 9. indexer logits over visible compressed blocks
  IndexerLogitsKernel<<<T, 256, 0, stream>>>(d_iq, idx_comp, d_logits,
                                             d_positions, T, n_iq, idx_hd,
                                             w.idx_compress, max_blocks);
  // 10. topk block selection -> token index list
  TopkSelectKernel<<<T, 256, 0, stream>>>(d_logits, d_topk, d_positions, T,
                                          w.idx_compress, w.idx_block_topk(),
                                          max_blocks, max_topk);
  // 11. sparse GQA attention over the selected positions
  SparseAttentionKernel<<<T * nq, 256, 0, stream>>>(d_q, kv_cache, page_table,
                                                    d_topk, d_attn, nq, nkv,
                                                    hd, max_topk);
  // 12. attn *= sigmoid(gate)
  GateMulKernel<<<(T * nq * hd + 255) / 256, 256, 0, stream>>>(
      d_attn, d_gate, T * nq * hd);
  // 13. out = attn @ W_o^T
  s = CheckGemm(Bf16Gemm(d_attn, w.o_proj, out, T, hs, nq * hd, 1.f, 0.f,
                         d_gemm_ws, gemm_ws, stream));
  cudaFreeAsync(d_positions, stream);
  return s;
}

}  // namespace model
}  // namespace q4t
