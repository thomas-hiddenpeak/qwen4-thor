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

#include <algorithm>
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

constexpr int kMaxT = 8192;  // max prefill tokens per attention forward
constexpr int kMaxBlocks = 2048;  // block-score CHUNK size (long ctx streams)
constexpr int kMaxBlockTopk = 512;  // idx_budget / compress (top-k blocks kept)
constexpr int kMaxTopk = 2052;  // idx_budget + compress - 1
// Max T for the one-pass long-context indexer (decode / MTP verify). At T <=
// kOnePassT the full [T, n_groups] logits buffer is small enough to materialise
// (T=1, 262K ctx -> 65536 f32 = 256 KB), so we score all blocks once and do a
// multi-level parallel top-k instead of the O(n_chunks) streaming merge.
constexpr int kOnePassT = 4;
// One-pass candidate-list stride: must hold the worst-case first-level output
// (ceil(kMaxGroups / kMaxBlocks) * kMaxBlockTopk). 262K ctx -> 65536 groups ->
// 32 slices * 512 = 16384. Bounded by kMaxOnePassCand below.
constexpr int kMaxGroups = 65536;  // 262144 / compress(4)
constexpr int kMaxOnePassCand =
    (kMaxGroups / kMaxBlocks) * kMaxBlockTopk;  // 32 slices * 512 = 16384

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

// Deterministic block reduction: sum of value[threadIdx.x] over the whole
// block, returned to every thread. The addition order is FIXED (warp shuffles
// in a fixed lane order, then warps added in a fixed sequence), so the result
// is bit-identical across runs — unlike float atomicAdd, whose completion
// order is nondeterministic and, since float addition is not associative,
// produced run-to-run differing RMSNorm sums (breaking bit-identical
// multi-sequence isolation). Requires blockDim.x % 32 == 0 (true for every
// caller: hd=256, idx_hd=128). kMaxThreads bounds the shared array; the
// runtime nwarps guard keeps it correct for 128- and 256-thread blocks.
__device__ __forceinline__ f32 BlockSum(f32 value) {
  constexpr int kWarp = 32;
  constexpr int kMaxThreads = 256;
  #pragma unroll
  for (int off = kWarp / 2; off > 0; off >>= 1)
    value += __shfl_xor_sync(0xffffffffu, value, off);
  __shared__ float s_warp[kMaxThreads / kWarp];
  const int lane = threadIdx.x & (kWarp - 1);
  const int wid = threadIdx.x / kWarp;
  if (lane == 0) s_warp[wid] = value;
  __syncthreads();
  const int nwarps = static_cast<int>(blockDim.x) / kWarp;
  value = 0.f;
  #pragma unroll
  for (int w = 0; w < kMaxThreads / kWarp; ++w)
    value += (w < nwarps) ? s_warp[w] : 0.f;
  return value;
}

Status CheckGemm(const Bf16GemmResult& r) {
  if (r.status != CUBLAS_STATUS_SUCCESS || !r.has_algo) {
    return Status::Fail(std::string("Bf16Gemm failed (status=") +
                        std::to_string(static_cast<int>(r.status)) + ")");
  }
  return Status();
}

// ---------------------------------------------------------------------------
// Kernel 1: fused q/k preprocessing. Fuses the old DeinterleaveQNormKernel +
// KNormKernel (two launches over the disjoint q / kv head sets of the qkv
// projections) into one launch. grid = T*(nq+nkv), block = hd threads:
//   blockIdx.x < T*nq  -> q head: deinterleave qg -> q, copy gate, centered
//                         RMSNorm(q)  (y = x*rsqrt(mean(x^2)+eps)*(1+w[d]))
//   otherwise          -> kv head: centered RMSNorm(k) in place
// Both branches are the same per-head centered RMSNorm over disjoint heads,
// so the result is bit-identical to the two-kernel path. The branch depends
// only on blockIdx.x (uniform within a block), so __syncthreads is safe.
__global__ void QKDeinterleaveNormKernel(const u16* __restrict__ qg,
                                         const u16* __restrict__ q_norm,
                                         u16* __restrict__ q,
                                         u16* __restrict__ gate, int nq,
                                         u16* __restrict__ k,
                                         const u16* __restrict__ k_norm,
                                         int T, int nkv, int hd, float eps) {
  const int th = blockIdx.x;
  const int d = threadIdx.x;
  if (d >= hd) return;
  if (th < T * nq) {
    const int t = th / nq;
    const int h = th % nq;
    const size_t base = (static_cast<size_t>(t) * nq + h) * (2 * hd);
    const f32 qv = Bf16ToFloat(qg[base + d]);
    // gate is a raw BF16 bit copy (no conversion).
    gate[static_cast<size_t>(t) * nq * hd + h * hd + d] = qg[base + hd + d];
    const float head_sum = BlockSum(qv * qv);
    const float rs = rsqrtf(head_sum / hd + eps);
    const f32 w = Bf16ToFloat(q_norm[d]);
    q[static_cast<size_t>(t) * nq * hd + h * hd + d] =
        FloatToBf16(qv * rs * (1.f + w));
  } else {
    const int h = th - T * nq;  // t * nkv + kv
    const int t = h / nkv;
    const int kv = h % nkv;
    const size_t base = (static_cast<size_t>(t) * nkv + kv) * hd;
    const f32 kvv = Bf16ToFloat(k[base + d]);
    const float head_sum = BlockSum(kvv * kvv);
    const float rs = rsqrtf(head_sum / hd + eps);
    const f32 w = Bf16ToFloat(k_norm[d]);
    k[base + d] = FloatToBf16(kvv * rs * (1.f + w));
  }
}

// ---------------------------------------------------------------------------
// Kernel 3: partial MRoPE (RoPE) on the first rot_d dims of q and k.
//
//   q : [T, nq, hd]  (in/out, first rot_d dims rotated)
//   k : [T, nkv, hd] (in/out)
//   rope_pos: [3, max_len] int32 — 3D MRoPE coordinates (t, h, w) for RoPE.
//
// RoPE: for i in [0, rot_d/2):
//   freq_i = theta^(-2i/rot_d)
//   q[i]   = q[i]*cos - q[i+half]*sin
//   q[i+half] = q[i]*sin + q[i+half]*cos
// One thread per (t, h, i) for i in [0, half).
// Interleaved MRoPE (transformers qwen4_exp apply_interleaved_mrope): for the
// i-th of the rot_d/2 frequency pairs, the position is chosen by i % 3:
//   i % 3 == 0 -> t-row, i % 3 == 1 -> h-row, i % 3 == 2 -> w-row.
// For pure text the three rows are identical (== logical position), so this
// reduces to standard RoPE on the first rot_d dims.
template <int N>
__global__ void PartialRopeKernel(u16* __restrict__ x, int n_heads, int hd,
                                  int rot_d, const int* __restrict__ positions,
                                  const int* __restrict__ rope_pos, int max_len,
                                  float theta, int T,
                                  const int* __restrict__ d_seq_id,
                                  int rope_seq_stride) {
  int half = rot_d / 2;
  int idx = blockIdx.x * blockDim.x + threadIdx.x;
  int total = T * N * half;
  if (idx >= total) return;
  int i = idx % half;
  int h = (idx / half) % N;
  int t = idx / (half * N);
  const int row = i % 3;  // 0=t, 1=h, 2=w (interleaved MRoPE)
  const int ap = positions[t];  // absolute position
  // B2 multi-seq: select this token's sequence [3, max_len] rope slice.
  const int* rope = d_seq_id
                        ? rope_pos + static_cast<size_t>(d_seq_id[t]) *
                                         rope_seq_stride
                        : rope_pos;
  float pos = static_cast<float>(rope[row * max_len + ap]);
  float inv_freq = powf(theta, -2.f * i / rot_d);
  float ang = pos * inv_freq;
  float c = cosf(ang), s = sinf(ang);
  size_t base = (static_cast<size_t>(t) * N + h) * hd + i;
  f32 a = Bf16ToFloat(x[base]);
  f32 b = Bf16ToFloat(x[base + half]);
  x[base] = FloatToBf16(a * c - b * s);
  x[base + half] = FloatToBf16(a * s + b * c);
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
                              const int* __restrict__ page_table, int T,
                              const int* __restrict__ d_seq_id,
                              size_t kv_seq_stride, int pt_seq_stride) {
  int idx = blockIdx.x * blockDim.x + threadIdx.x;
  int total = T * nkv * hd;
  if (idx >= total) return;
  int d = idx % hd;
  int h = (idx / hd) % nkv;
  int t = idx / (hd * nkv);
  int pos = positions[t];
  // B2 multi-seq: select this token's sequence KV + page_table slices.
  u16* kv = d_seq_id
                ? kv_cache + static_cast<size_t>(d_seq_id[t]) * kv_seq_stride
                : kv_cache;
  const int* pt = d_seq_id
                      ? page_table + static_cast<size_t>(d_seq_id[t]) * pt_seq_stride
                      : page_table;
  size_t src = (static_cast<size_t>(t) * nkv + h) * hd + d;
  int slot = pt[pos] * kKvPageSize + (pos % kKvPageSize);
  size_t dst =
      (static_cast<size_t>(slot) * nkv + h) * (2 * hd);
  kv[dst + d] = k[src];
  kv[dst + hd + d] = v[src];
}

// ---------------------------------------------------------------------------
// Kernel 5: centered GemmaRMSNorm + partial RoPE on indexer queries.
// Raw keys bypass this transform: compressed keys are normalized after pooling.
// One block per (token, query head), one thread per dimension.
__global__ void IndexerQueryNormRopeKernel(
    u16* __restrict__ iq, const u16* __restrict__ iq_norm,
    const int* __restrict__ positions, const int* __restrict__ rope_pos,
    int max_len, int n_iq, int hd, int rot_d, float theta, float eps,
    const int* __restrict__ d_seq_id, int rope_seq_stride) {
  const int t = blockIdx.x / n_iq;
  const int head = blockIdx.x % n_iq;
  const int d = threadIdx.x;
  u16* x = iq + (static_cast<size_t>(t) * n_iq + head) * hd;
  const u16* nw = iq_norm;
  if (d >= hd) return;
  f32 xv = Bf16ToFloat(x[d]);
  const float head_sum = BlockSum(xv * xv);
  float rs = rsqrtf(head_sum / hd + eps);
  f32 w = 1.f + Bf16ToFloat(nw[d]);
  x[d] = FloatToBf16(xv * rs * w);
  // The rotary pair spans warps; both normalized values must be visible.
  __syncthreads();
  // partial RoPE on first rot_d dims (interleaved MRoPE: row = d % 3).
  int half = rot_d / 2;
  if (d < half) {
    const int row = d % 3;  // 0=t, 1=h, 2=w
    const int ap = positions[t];  // absolute position
    const int* rope = d_seq_id
                          ? rope_pos + static_cast<size_t>(d_seq_id[t]) *
                                           rope_seq_stride
                          : rope_pos;
    float pos = static_cast<float>(rope[row * max_len + ap]);
    float inv_freq = powf(theta, -2.f * d / rot_d);
    float ang = pos * inv_freq;
    float c = cosf(ang), s = sinf(ang);
    f32 a = Bf16ToFloat(x[d]);
    f32 b = Bf16ToFloat(x[d + half]);
    x[d] = FloatToBf16(a * c - b * s);
    x[d + half] = FloatToBf16(a * s + b * c);
  }
}

// ---------------------------------------------------------------------------
// Kernel 6: store raw index keys (pre-RoPE) per token + build compressed keys.
//
// The raw ik (pre-RoPE, pre-norm) is stored per token in idx_raw. When a
// group of `compress` tokens completes, the group's average (FP32) is
// GemmaRMSNorm'd and RoPE'd at the group's first position -> idx_comp.
//
// This kernel is launched once per token position t (grid = T). For each t:
//   - copy ik[t] (pre-norm, pre-RoPE) into idx_raw[pos]
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
                                    int hd, const int* __restrict__ d_seq_id,
                                    size_t idx_seq_stride) {
  int t = blockIdx.x;
  if (t >= T) return;
  int pos = positions[t];
  int d = threadIdx.x;
  if (d >= hd) return;
  // B2 multi-seq: select this token's sequence idx_raw slice.
  u16* idx = d_seq_id
                 ? idx_raw + static_cast<size_t>(d_seq_id[t]) * idx_seq_stride
                 : idx_raw;
  idx[static_cast<size_t>(pos) * hd + d] = ik_raw[static_cast<size_t>(t) * hd + d];
}

__global__ void BuildCompressedKKernel(const u16* __restrict__ ik_norm,
                                       const u16* __restrict__ idx_raw,
                                       u16* __restrict__ idx_comp,
                                       const int* __restrict__ positions,
                                       const int* __restrict__ rope_pos, int T,
                                       int max_len, int hd, int compress,
                                       float theta, float eps,
                                       const int* __restrict__ d_seq_id,
                                       size_t idx_seq_stride,
                                       int rope_seq_stride) {
  int t = blockIdx.x;
  if (t >= T) return;
  int pos = positions[t];
  int d = threadIdx.x;
  if (d >= hd) return;
  // B2 multi-seq: select this token's sequence idx_raw / idx_comp / rope
  // slices (the group's raw tokens all belong to the same sequence).
  const u16* iraw = d_seq_id
                        ? idx_raw + static_cast<size_t>(d_seq_id[t]) * idx_seq_stride
                        : idx_raw;
  u16* icomp = d_seq_id
                   ? idx_comp + static_cast<size_t>(d_seq_id[t]) * idx_seq_stride
                   : idx_comp;
  const int* rope = d_seq_id
                        ? rope_pos + static_cast<size_t>(d_seq_id[t]) *
                                         rope_seq_stride
                        : rope_pos;
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
    acc += Bf16ToFloat(iraw[static_cast<size_t>(g0 + j) * hd + d]);
  }
  acc /= compress;
  // GemmaRMSNorm (centered) over the head: need sum of squares
  const float head_sum = BlockSum(acc * acc);
  float rs = rsqrtf(head_sum / hd + eps);
  f32 w = 1.f + Bf16ToFloat(ik_norm[d]);
  float normed = acc * rs * w;
  // partial RoPE at the group's first position g0, using the PERSISTENT
  // 3D MRoPE table (rope_pos has max_len entries per row, so g0 is always
  // in range — even during decode, where the batch positions array holds a
  // single element). Interleaved MRoPE: row = d % 3 (0=t, 1=h, 2=w).
  int half = 64 / 2;  // rot_d = 64 for the indexer (idx_head_dim 128, factor .5)
  const int row = d % 3;
  float pos0 = static_cast<float>(rope[row * max_len + g0]);
  float val;
  if (d < half) {
    float inv_freq = powf(theta, -2.f * d / 64.f);
    float ang = pos0 * inv_freq;
    float c = cosf(ang), s = sinf(ang);
    // need the partner (d+half) normed value; recompute it
    float acc2 = 0.f;
    for (int j = 0; j < compress; ++j) {
      acc2 += Bf16ToFloat(iraw[static_cast<size_t>(g0 + j) * hd + (d + half)]);
    }
    acc2 /= compress;
    float w2 = 1.f + Bf16ToFloat(ik_norm[d + half]);
    float normed2 = acc2 * rs * w2;
    val = normed * c - normed2 * s;
  } else if (d >= half && d < 64) {
    int d2 = d - half;
    float inv_freq = powf(theta, -2.f * d2 / 64.f);
    float ang = pos0 * inv_freq;
    float c = cosf(ang), s = sinf(ang);
    float acc2 = 0.f;
    for (int j = 0; j < compress; ++j) {
      acc2 += Bf16ToFloat(iraw[static_cast<size_t>(g0 + j) * hd + d2]);
    }
    acc2 /= compress;
    float w2 = 1.f + Bf16ToFloat(ik_norm[d2]);
    float normed2 = acc2 * rs * w2;
    val = normed2 * s + normed * c;
  } else {
    val = normed;  // dims >= 64 unchanged
  }
  icomp[static_cast<size_t>(group) * hd + d] = FloatToBf16(val);
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
                                    int max_blocks, int block_off,
                                    const int* __restrict__ d_seq_id,
                                    size_t idx_seq_stride) {
  int t = blockIdx.x;
  if (t >= T) return;
  const int pos = positions[t];
  const int n_groups = (pos + 1) / compress;  // global visible groups (uncapped)
  const float inv_sqrt = 1.f / sqrtf(static_cast<float>(hd));
  const u16* cks = d_seq_id
                       ? ck + static_cast<size_t>(d_seq_id[t]) * idx_seq_stride
                       : ck;
  // Scores the chunk of GLOBAL blocks [block_off, block_off + max_blocks);
  // logits written at the local column lg = g - block_off.
  for (int lg = threadIdx.x; lg < max_blocks; lg += blockDim.x) {
    const int g = block_off + lg;
    float val = -1e30f;
    if (g < n_groups) {
      float sum = 0.f;
      for (int h = 0; h < n_iq; ++h) {
        float dot = 0.f;
        for (int d = 0; d < hd; ++d)
          dot += Bf16ToFloat(iq[(static_cast<size_t>(t) * n_iq + h) * hd + d]) *
                 Bf16ToFloat(cks[static_cast<size_t>(g) * hd + d]);
        sum += fmaxf(dot, 0.f);
      }
      val = sum * inv_sqrt;
    }
    logits[static_cast<size_t>(t) * max_blocks + lg] = val;
  }
}

// Kernel 7b: indexer logits reduction over the tensor-core GEMM output.
// S[t*n_iq+h, g] = iq[t,h] . ck[g] (from Bf16Gemm). This folds the head relu +
// sum + 1/sqrt(hd) scale + causal group mask that the SIMT IndexerLogitsKernel
// did inline. One thread per (t, g); logits[t,g] = -1e30 for invisible groups
// (g >= (pos+1)/compress), whose S columns are never read (single-seq path).
__global__ void IndexerReduceKernel(const u16* __restrict__ S,
                                    f32* __restrict__ logits,
                                    const int* __restrict__ positions, int T,
                                    int n_iq, int hd, int compress,
                                    int max_blocks, int block_off) {
  const int idx = blockIdx.x * blockDim.x + threadIdx.x;
  if (idx >= T * max_blocks) return;
  const int t = idx / max_blocks;
  const int lg = idx % max_blocks;
  const int g = block_off + lg;  // global block index
  const int n_groups = (positions[t] + 1) / compress;  // uncapped
  if (g >= n_groups) {
    logits[idx] = -1e30f;
    return;
  }
  const float inv_sqrt = rsqrtf(static_cast<float>(hd));
  float sum = 0.f;
  for (int h = 0; h < n_iq; ++h) {
    const float dot =
        Bf16ToFloat(S[(static_cast<size_t>(t) * n_iq + h) * max_blocks + lg]);
    sum += fmaxf(dot, 0.f);
  }
  logits[idx] = sum * inv_sqrt;
}

// ---------------------------------------------------------------------------
// Kernel 8: topk block selection -> token index list.
//
// Dense regime (n_visible_blocks <= block_topk): all visible positions are
// selected (QSA == dense causal). Sparse regime: top-`block_topk` blocks by
// logit, expanded to tokens, + the current group's tail tokens.
//
//   topk : [T, max_topk] int32 (out) — selected token positions
//   topk_len : [T] int32 (out) — number of valid (non -1) positions per token
//   logits: [T, max_blocks] FP32
//   positions: [T]
// block = one token t, 256 threads.
//
// PARALLEL (2026-09-11): the old implementation ran the sparse top-k on a
// SINGLE thread (O(block_topk x n_groups) sequential global reads: 512 x
// n_groups, n_groups = O(seq_len)). nsys at 4K/8K context showed it was the
// #1 decode kernel: 11.7ms (4K) / 20.4ms (8K) per call, 61%/73% of all GPU
// time — this, not KV reads, was why decode degraded like a dense full-
// attention model. Now: load the (logit, block index) pairs into shared
// memory (padding = -inf so it sorts to the front and is never selected),
// in-place bitonic sort ASCENDING (2048 = 2^11, 11 stages, 256 threads),
// the top `block_topk` blocks are the last `block_topk` entries, expand to
// tokens. Padding (-1e30f) sorts below valid scores in the sparse regime
// where n_groups > block_topk. Equal scores retain the ordering produced by
// the fixed sort network; this change does not introduce a new tie-break.
// Keep expansion order deterministic: floating-point attention reductions
// depend on list order even when the selected set is unchanged.
__global__ void TopkSelectKernel(f32* __restrict__ logits, int* __restrict__ topk,
                                 int* __restrict__ topk_len,
                                 const int* __restrict__ positions, int T,
                                 int compress, int block_topk, int max_blocks,
                                 int max_topk) {
  __shared__ float s_val[2048];  // kMaxBlocks
  __shared__ int s_idx[2048];
  const int t = blockIdx.x;
  if (t >= T) return;
  const int pos = positions[t];
  const int n_groups = (pos + 1) / compress > max_blocks
                           ? max_blocks
                           : (pos + 1) / compress;
  int* out = topk + static_cast<size_t>(t) * max_topk;
  const f32* lrow = logits + static_cast<size_t>(t) * max_blocks;
  int n;
  if (n_groups <= block_topk) {
    // dense: all visible positions (QSA == dense causal attention)
    for (int p = threadIdx.x; p <= pos; p += 256) out[p] = p;
    n = pos + 1;
  } else {
    // sparse: load (logit, block index) pairs; pad with -inf so padding
    // sorts to the front (ascending) and is never among the top block_topk.
    for (int g = threadIdx.x; g < 2048; g += 256) {
      s_val[g] = (g < n_groups) ? lrow[g] : -1e30f;
      s_idx[g] = g;
    }
    __syncthreads();
    // In-place bitonic sort of (s_val, s_idx) pairs, ascending, N = 2048 =
    // 2^11. Standard network: for block-size k = 2..N, stride j = k/2..1,
    // compare-exchange (i, i+j) for every i in the FIRST HALF of each 2j-block
    // ((i & (2j-1)) < j); ascending within the first k/2 of each 2k-superblock
    // ((i & (2k-1)) < k), descending in the second half.
#pragma unroll
    for (int k = 2; k <= 2048; k <<= 1) {
#pragma unroll
      for (int j = k >> 1; j > 0; j >>= 1) {
        for (int i = threadIdx.x; i < 2048; i += 256) {
          if ((i & (2 * j - 1)) >= j) continue;  // only first half of 2j-block
          const int p = i + j;
          const bool asc = ((i & (2 * k - 1)) < k);
          if (asc ? (s_val[i] > s_val[p]) : (s_val[i] < s_val[p])) {
            const float tv = s_val[i];
            s_val[i] = s_val[p];
            s_val[p] = tv;
            const int ti = s_idx[i];
            s_idx[i] = s_idx[p];
            s_idx[p] = ti;
          }
        }
        __syncthreads();
      }
    }
    // Expand in sorted-block order. Atomic slot allocation changes the
    // floating-point reduction order in SparseAttentionKernel across runs.
    for (int c = threadIdx.x; c < block_topk; c += 256) {
      const int g = s_idx[2048 - block_topk + c];
      const int base = g * compress;
      for (int j = 0; j < compress; ++j) {
        const int p = base + j;
        out[c * compress + j] = p;
      }
    }
    // Complete groups are governed solely by top-k selection. Only the
    // unfinished group's visible tail is appended (at most compress - 1).
    const int tail = (pos + 1) % compress;
    n = block_topk * compress;
    if (threadIdx.x < tail)
      out[n + threadIdx.x] = pos + 1 - tail + threadIdx.x;
    n += tail;
  }
  for (int i = n + threadIdx.x; i < max_topk; i += 256) out[i] = -1;
  if (threadIdx.x == 0) topk_len[t] = n;
}

// ---------------------------------------------------------------------------
// Streaming block top-k (long context, > kMaxBlocks * compress tokens).
//
// The single-shot TopkSelectKernel above bitonic-sorts logits over a single
// [T, kMaxBlocks] buffer, so it can only ever consider the first kMaxBlocks
// (2048) compressed blocks -> the first 8192 tokens. For agent-scale prompts
// (40K-200K tokens) that silently drops all recent context from the sparse
// candidate set, breaking recall. Materializing logits over ALL blocks
// ([T, n_groups]) is infeasible for prefill (T=8192, n_groups up to ~50000).
//
// Instead we stream, mirroring tokenspeed's split + merge-tree QSA kernels:
// score the blocks in CHUNK = kMaxBlocks slices (tensor-core GEMM per slice),
// keep a running per-query top-block_topk, and merge each chunk's local
// top-block_topk into it. Memory stays bounded ([T, block_topk]); the final
// run_idx holds the global top-block_topk over the entire history.
// ---------------------------------------------------------------------------

// Bitonic sort (ascending) of (val,idx) held in shared memory. N must be a
// power of two and <= blockDim.x-addressable; after the sort the k largest
// entries are the last k. Runtime N (no unroll) — correctness over speed;
// cost grows with context and query count (see the HTTP timeline report).
__device__ void BitonicSortAsc(float* v, int* id, int N) {
  for (int k = 2; k <= N; k <<= 1) {
    for (int j = k >> 1; j > 0; j >>= 1) {
      for (int i = threadIdx.x; i < N; i += blockDim.x) {
        if ((i & (2 * j - 1)) >= j) continue;
        const int p = i + j;
        const bool asc = ((i & (2 * k - 1)) < k);
        if (asc ? (v[i] > v[p]) : (v[i] < v[p])) {
          const float tv = v[i];
          v[i] = v[p];
          v[p] = tv;
          const int ti = id[i];
          id[i] = id[p];
          id[p] = ti;
        }
      }
      __syncthreads();
    }
  }
}

// Initialise the running top-k to empty (score -inf, id -1).
__global__ void InitRunTopkKernel(f32* __restrict__ run_val,
                                  int* __restrict__ run_idx, int total) {
  const int i = blockIdx.x * blockDim.x + threadIdx.x;
  if (i >= total) return;
  run_val[i] = -1e30f;
  run_idx[i] = -1;
}

// Merge one chunk's block logits into the running per-query top-block_topk.
// logits_c: [T, max_blocks] scores for global blocks [block_off, block_off +
// max_blocks) (invisible / out-of-range columns are -1e30). One block per
// query t, 256 threads. block_topk <= kMaxBlockTopk, max_blocks == kMaxBlocks.
__global__ void MergeChunkTopkKernel(const f32* __restrict__ logits_c,
                                     int block_off, int max_blocks,
                                     int block_topk, int T,
                                     f32* __restrict__ run_val,
                                     int* __restrict__ run_idx) {
  const int t = blockIdx.x;
  if (t >= T) return;
  __shared__ float s_val[kMaxBlocks];
  __shared__ int s_idx[kMaxBlocks];
  __shared__ float m_val[2 * kMaxBlockTopk];
  __shared__ int m_idx[2 * kMaxBlockTopk];
  const f32* lrow = logits_c + static_cast<size_t>(t) * max_blocks;
  for (int g = threadIdx.x; g < kMaxBlocks; g += blockDim.x) {
    s_val[g] = (g < max_blocks) ? lrow[g] : -1e30f;
    s_idx[g] = block_off + g;
  }
  __syncthreads();
  // Local top-block_topk of this chunk -> last block_topk after the sort.
  BitonicSortAsc(s_val, s_idx, kMaxBlocks);
  // Merge running top-k (first half) with this chunk's top-k (second half).
  for (int i = threadIdx.x; i < block_topk; i += blockDim.x) {
    m_val[i] = run_val[static_cast<size_t>(t) * block_topk + i];
    m_idx[i] = run_idx[static_cast<size_t>(t) * block_topk + i];
    m_val[block_topk + i] = s_val[kMaxBlocks - block_topk + i];
    m_idx[block_topk + i] = s_idx[kMaxBlocks - block_topk + i];
  }
  __syncthreads();
  BitonicSortAsc(m_val, m_idx, 2 * block_topk);
  for (int i = threadIdx.x; i < block_topk; i += blockDim.x) {
    run_val[static_cast<size_t>(t) * block_topk + i] = m_val[block_topk + i];
    run_idx[static_cast<size_t>(t) * block_topk + i] = m_idx[block_topk + i];
  }
}

// Block-parallel streaming indexer kernels (decode / small T).
//
// The token-parallel kernels above (IndexerLogitsKernel / MergeChunkTopkKernel)
// launch one 256-thread block per TOKEN. For decode (T=1) that is a SINGLE
// block scoring 2048 blocks serially on 20 SMs (~1/160 utilization) — the
// dominant decode cost at long context (measured: 704us/chunk x 15 chunks x
// 12 layers ~= 127ms/step). These kernels parallelize over BLOCK TILES instead
// (grid.x = 2048/256 = 8 tiles, grid.y = T), mirroring vllm's decode QSA
// indexer (program_id over block tiles). Each thread scores ONE block (all
// n_iq heads), so 2048 blocks use 2048 threads across all SMs.
//
// Valid for small T (<= 4) with OR without d_seq_id: for T <= 4 each token's
// d_seq_id[t] is fixed, so the per-sequence idx_comp slice can be selected
// once per block (the query rows are still shared across the block's tiles).
// Large T (prefill) keeps the tensor-core GEMM scoring path.

// Score one 2048-block chunk in parallel. iq: [T, n_iq, hd] (RoPE'd),
// ck: idx_comp (+ per-seq slice via d_seq_id) + block_off*hd, logits_c:
// [T, max_blocks].
__global__ void IndexerLogitsBlockParKernel(
    const u16* __restrict__ iq, const u16* __restrict__ ck,
    f32* __restrict__ logits_c, const int* __restrict__ positions, int T,
    int n_iq, int hd, int compress, int max_blocks, int block_off,
    const int* __restrict__ d_seq_id, size_t idx_seq_stride) {
  const int t = blockIdx.y;
  if (t >= T) return;
  const int tile = blockIdx.x;
  const int g = tile * 256 + threadIdx.x;  // LOCAL block index within the chunk
  const int pos = positions[t];
  const int n_groups = (pos + 1) / compress;  // GLOBAL visible block count
  const int global_g = block_off + g;  // global block index (ck is pre-offset)
  const float inv_sqrt = 1.f / sqrtf(static_cast<float>(hd));
  // Per-sequence idx_comp slice (T <= 4: d_seq_id[t] is fixed for this token).
  const u16* cks = d_seq_id
                       ? ck + static_cast<size_t>(d_seq_id[t]) * idx_seq_stride
                       : ck;
  // Stage this token's n_iq query rows. n_iq <= 4, hd <= 128 (idx config), so
  // a fixed 512-element buffer covers it (VLA shared memory is illegal).
  __shared__ u16 s_q[512];
  for (int i = threadIdx.x; i < n_iq * hd; i += blockDim.x)
    s_q[i] = iq[(static_cast<size_t>(t) * n_iq * hd) + i];
  __syncthreads();
  float val = -1e30f;
  if (global_g < n_groups) {
    float sum = 0.f;
    for (int h = 0; h < n_iq; ++h) {
      float dot = 0.f;
      for (int d = 0; d < hd; ++d)
        dot += Bf16ToFloat(s_q[h * hd + d]) *
               Bf16ToFloat(cks[static_cast<size_t>(g) * hd + d]);
      sum += fmaxf(dot, 0.f);
    }
    val = sum * inv_sqrt;
  }
  // Write to the LOCAL column g (MergeChunkTopkKernel reads [0, max_blocks)).
  logits_c[static_cast<size_t>(t) * max_blocks + g] = val;
}

// Expand the running top-block_topk block ids to visible token positions plus
// the current (in-progress) group tail. Mirrors TopkSelectKernel's sparse
// expand, but reads the streamed run_idx / run_val instead of sorting logits.
__global__ void ExpandRunTopkKernel(const int* __restrict__ run_idx,
                                    const f32* __restrict__ run_val,
                                    const int* __restrict__ positions, int T,
                                    int compress, int block_topk,
                                    int* __restrict__ topk,
                                    int* __restrict__ topk_len, int max_topk) {
  const int t = blockIdx.x;
  if (t >= T) return;
  const int pos = positions[t];
  int* out = topk + static_cast<size_t>(t) * max_topk;
  const int* rid = run_idx + static_cast<size_t>(t) * block_topk;
  const f32* rval = run_val + static_cast<size_t>(t) * block_topk;
  int valid_count = 0;
  for (int base = 0; base < block_topk; base += blockDim.x) {
    const int c = base + threadIdx.x;
    const bool valid = c < block_topk && rval[c] > -1e29f;
    valid_count += __syncthreads_count(valid);
  }
  // Merge output is ascending: empty slots precede all valid blocks.
  const int first = block_topk - valid_count;
  // Fixed slots preserve attention reduction order across launches.
  for (int c = first + threadIdx.x; c < block_topk; c += blockDim.x) {
    const int base = rid[c] * compress;
    for (int j = 0; j < compress; ++j)
      out[(c - first) * compress + j] = base + j;
  }
  int n = (block_topk - first) * compress;
  const int tail = (pos + 1) % compress;
  if (threadIdx.x < tail)
    out[n + threadIdx.x] = pos + 1 - tail + threadIdx.x;
  n += tail;
  for (int i = n + threadIdx.x; i < max_topk; i += blockDim.x) out[i] = -1;
  if (threadIdx.x == 0) topk_len[t] = n;
}

// ---------------------------------------------------------------------------
// One-pass long-context indexer (decode / small T, T <= kOnePassT).
//
// The streaming path above exists because PREFILL (T up to 8192) cannot
// materialise [T, n_groups] logits. But at decode (T <= 4) the full
// [T, n_groups] is tiny (T=1, 262K context -> 65536 f32 = 256 KB), so we can
// score ALL blocks in ONE pass and select the global top-block_topk with a
// small multi-level parallel reduction. This removes the O(n_chunks) cost of
// the streaming merge (6 chunks x 12 layers x 132us ~= 9.5ms/step at 30K)
// and the under-parallelised chunked scoring.
//
// Correctness: the selected SET is the exact global top-block_topk. Lemma:
// global top-K  subset  union over windows of (window top-K), because if a
// global top-K element x were absent from its window's top-K, that window
// would hold >= K elements > x, contradicting x being within the global top-K.
// The invariant is preserved at every reduction level (each window keeps
// top-block_topk), so the final sort yields the exact global top-block_topk.
// The scoring math is identical to IndexerLogitsBlockParKernel
// (sum_h relu(q_h . k_g) / sqrt(hd)), so selection is bit-equivalent to the
// streaming path up to measure-zero FP32 ties.

// Score every visible block for each token in one pass. One WARP per block
// (8 blocks per 256-thread CUDA block), coalesced key reads (lane l reads key
// dims {l, 32+l, 64+l, 96+l}). iq: [T, n_iq, hd] (RoPE'd); ck: idx_comp
// (+ per-seq slice via d_seq_id); logits: [T, n_groups] f32 (OOB / causal-
// hidden blocks are -1e30).
__global__ void OnePassScoreKernel(const u16* __restrict__ iq,
                                   const u16* __restrict__ ck,
                                   f32* __restrict__ logits,
                                   const int* __restrict__ positions, int T,
                                   int n_iq, int hd, int compress,
                                   int n_groups,
                                   const int* __restrict__ d_seq_id,
                                   size_t idx_seq_stride) {
  const int t = blockIdx.y;
  if (t >= T) return;
  const int warp = threadIdx.x >> 5;
  const int lane = threadIdx.x & 31;
  const int g = blockIdx.x * 8 + warp;  // global block index for this warp
  const int pos = positions[t];
  const int n_vis = (pos + 1) / compress;  // visible blocks for this token
  const float inv_sqrt = 1.f / sqrtf(static_cast<float>(hd));
  const u16* cks = d_seq_id
                       ? ck + static_cast<size_t>(d_seq_id[t]) * idx_seq_stride
                       : ck;
  // Stage this token's query [n_iq, hd] (n_iq*hd <= 512 for the idx config).
  __shared__ u16 s_q[512];
  for (int i = threadIdx.x; i < n_iq * hd; i += blockDim.x)
    s_q[i] = iq[(static_cast<size_t>(t) * n_iq * hd) + i];
  __syncthreads();
  float acc = 0.f;
  if (g < n_groups && g < n_vis) {
    const u16* krow = cks + static_cast<size_t>(g) * hd;
    for (int h = 0; h < n_iq; ++h) {
      float dot = 0.f;
#pragma unroll
      for (int m = 0; m < hd / 32; ++m) {
        const int d = m * 32 + lane;
        dot += Bf16ToFloat(s_q[h * hd + d]) * Bf16ToFloat(krow[d]);
      }
#pragma unroll
      for (int off = 16; off > 0; off >>= 1)
        dot += __shfl_down_sync(0xffffffffu, dot, off);
      if (lane == 0) acc += fmaxf(dot, 0.f);
    }
  }
  // grid.x = ceil(n_groups/8), so tail warps can have g >= n_groups: guard the
  // write (logits is sized exactly [T, n_groups]).
  if (lane == 0 && g < n_groups)
    logits[static_cast<size_t>(t) * n_groups + g] =
        (g < n_vis) ? acc * inv_sqrt : -1e30f;
}

// Local top-block_topk of one 2048-wide slice of the one-pass logits.
// grid = (num_slices, T). logits: [T, n_groups]; out (val,idx): [T, stride]
// with this slice's top-block_topk at [slice*block_topk, +block_topk).
__global__ void SliceLocalTopkKernel(const f32* __restrict__ logits,
                                     int n_groups, int max_blocks,
                                     int block_topk, int T,
                                     f32* __restrict__ out_val,
                                     int* __restrict__ out_idx, int stride) {
  const int t = blockIdx.y;
  if (t >= T) return;
  const int base = blockIdx.x * max_blocks;
  __shared__ float s_val[kMaxBlocks];
  __shared__ int s_idx[kMaxBlocks];
  const f32* lrow = logits + static_cast<size_t>(t) * n_groups;
  for (int g = threadIdx.x; g < kMaxBlocks; g += blockDim.x) {
    const int gg = base + g;
    s_val[g] = (gg < n_groups) ? lrow[gg] : -1e30f;
    s_idx[g] = gg;
  }
  __syncthreads();
  BitonicSortAsc(s_val, s_idx, kMaxBlocks);
  __syncthreads();
  for (int i = threadIdx.x; i < block_topk; i += blockDim.x) {
    const size_t o = static_cast<size_t>(t) * stride + blockIdx.x * block_topk + i;
    out_val[o] = s_val[kMaxBlocks - block_topk + i];
    out_idx[o] = s_idx[kMaxBlocks - block_topk + i];
  }
}

// Merge level: local top-block_topk of each 2048-window of the current
// candidate list (val,idx). grid = (ceil(C/max_blocks), T). Reads C candidates
// from in (stride), writes ceil(C/max_blocks)*block_topk to out (stride).
__global__ void WindowMergeTopkKernel(const f32* __restrict__ in_val,
                                      const int* __restrict__ in_idx, int C,
                                      int in_stride, int max_blocks,
                                      int block_topk, int T,
                                      f32* __restrict__ out_val,
                                      int* __restrict__ out_idx,
                                      int out_stride) {
  const int t = blockIdx.y;
  if (t >= T) return;
  const int base = blockIdx.x * max_blocks;
  __shared__ float s_val[kMaxBlocks];
  __shared__ int s_idx[kMaxBlocks];
  const f32* iv = in_val + static_cast<size_t>(t) * in_stride;
  const int* ii = in_idx + static_cast<size_t>(t) * in_stride;
  for (int g = threadIdx.x; g < kMaxBlocks; g += blockDim.x) {
    const int k = base + g;
    s_val[g] = (k < C) ? iv[k] : -1e30f;
    s_idx[g] = (k < C) ? ii[k] : -1;
  }
  __syncthreads();
  BitonicSortAsc(s_val, s_idx, kMaxBlocks);
  __syncthreads();
  for (int i = threadIdx.x; i < block_topk; i += blockDim.x) {
    const size_t o = static_cast<size_t>(t) * out_stride +
                     blockIdx.x * block_topk + i;
    out_val[o] = s_val[kMaxBlocks - block_topk + i];
    out_idx[o] = s_idx[kMaxBlocks - block_topk + i];
  }
}

// Final: one block per token. Sort the (<= 2048) surviving candidates, take
// top-block_topk, expand to token positions + force-include the current group
// tail. Mirrors ExpandRunTopkKernel but reads the one-pass candidate list.
__global__ void FinalTopkExpandKernel(const f32* __restrict__ cand_val,
                                      const int* __restrict__ cand_idx, int C,
                                      int stride,
                                      const int* __restrict__ positions, int T,
                                      int compress, int block_topk,
                                      int* __restrict__ topk,
                                      int* __restrict__ topk_len, int max_topk) {
  const int t = blockIdx.x;
  if (t >= T) return;
  const int pos = positions[t];
  __shared__ float s_val[kMaxBlocks];
  __shared__ int s_idx[kMaxBlocks];
  const f32* cv = cand_val + static_cast<size_t>(t) * stride;
  const int* ci = cand_idx + static_cast<size_t>(t) * stride;
  for (int g = threadIdx.x; g < kMaxBlocks; g += blockDim.x) {
    s_val[g] = (g < C) ? cv[g] : -1e30f;
    s_idx[g] = (g < C) ? ci[g] : -1;
  }
  __syncthreads();
  BitonicSortAsc(s_val, s_idx, kMaxBlocks);
  __syncthreads();
  int* out = topk + static_cast<size_t>(t) * max_topk;
  int valid_count = 0;
  for (int base = kMaxBlocks - block_topk; base < kMaxBlocks;
       base += blockDim.x) {
    const int c = base + threadIdx.x;
    const bool valid = c < kMaxBlocks && s_val[c] > -1e29f;
    valid_count += __syncthreads_count(valid);
  }
  const int first = kMaxBlocks - valid_count;
  for (int c = first + threadIdx.x; c < kMaxBlocks; c += blockDim.x) {
    const int base = s_idx[c] * compress;
    for (int j = 0; j < compress; ++j)
      out[(c - first) * compress + j] = base + j;
  }
  int n = (kMaxBlocks - first) * compress;
  const int tail = (pos + 1) % compress;
  if (threadIdx.x < tail)
    out[n + threadIdx.x] = pos + 1 - tail + threadIdx.x;
  n += tail;
  for (int i = n + threadIdx.x; i < max_topk; i += blockDim.x) out[i] = -1;
  if (threadIdx.x == 0) topk_len[t] = n;
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
// Tensor-core GQA-packed rewrite (replaces the per-q-head SIMT kernel).
//
// The old kernel launched one 256-thread block per (t, qh): grid T*nq. With
// nkv=2 / nq=24 (GQA 12:1), the same KV row was read from memory 12 times —
// once per q-head sharing a kv-head. On Thor (LPDDR5x, 241 GB/s) the real
// prefill is memory-bound, so that 12x redundant KV read is the dominant
// cost (214 GB vs 18 GB). This kernel packs all 12 q-heads of one kv-head
// into a single block: grid T*nkv. Each block reads its KV chunk ONCE and
// feeds all 12 q-heads through mma.sync tensor-core matmuls, mirroring
// vllm's _qsa_sparse_paged_gqa_splitk_kernel (tl.dot on [12,hd]x[hd,BN]).
//
// Layout: block = 256 threads = 8 warps. Warp w handles q-heads
// m0 = 2*w, m0+1 (m = qh - kvh*12 in [0,12)). Per 16-position chunk:
//   sQ [12, 256] bf16  — the 12 q-heads' query rows (row-major)
//   sK [16, 256] bf16  — K rows for the chunk (row-major)
//   sV [16, 256] bf16  — V rows for the chunk (row-major)
//   sS [12, 16]  f32   — scores = sQ @ sK^T (via mma m16n8k16)
//   sP [16, 12]  bf16  — softmax probs (transposed for the PV mma B operand)
//   sO [12, 256] f32   — running PV accumulator (rescaled by alpha)
// QK^T: per 8-dim n-tile, mma A = sQ rows (16 m, 12 used), B = sK cols.
// PV:   per 8-dim n-tile, mma A = sP (16 m = positions, 12 used), B = sV cols.
// Online softmax (running max/sum) is applied to sS per chunk; the running
// sO accumulator is rescaled by alpha = exp(m_old - m_new) each chunk.
//
// ldmatrix loads the mma fragments from shared memory (no bank conflicts for
// row-major bf16 with the standard x4 pattern). The PV B-operand (sV^T view)
// is loaded with ldmatrix.trans.
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

// cp.async: asynchronously copy 16 bytes global->shared (Ampere+; SM110
// Blackwell). Used to prefetch the next chunk's scattered paged-KV while the
// current chunk computes, hiding the LPDDR5x scatter latency that makes the
// per-chunk gather->compute serialization the dominant prefill cost.
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

__global__ void SparseAttentionKernel(const u16* __restrict__ q,
                                      const u16* __restrict__ kv_cache,
                                      const int* __restrict__ page_table,
                                      const int* __restrict__ topk,
                                      const int* __restrict__ topk_len,
                                      u16* __restrict__ out,
                                      const u16* __restrict__ gate, int nq,
                                      int nkv, int hd, int max_topk,
                                      const int* __restrict__ d_seq_id,
                                      size_t kv_seq_stride, int pt_seq_stride) {
  // Tensor-core GQA-packed QSA attention. grid = (T, nkv); each block reads
  // its kv-head's K/V ONCE and drives all 12 q-heads through mma.sync
  // (12x KV-read reduction vs the old per-q-head kernel). Tile shape is fixed
  // to the model constants (hd=256, nq=24, nkv=2 -> G=12); a runtime guard
  // rejects any other geometry.
  constexpr int kG = 12;      // q-heads per kv-head
  constexpr int kHd = 256;
  constexpr int kChunk = 16;  // positions processed per mma pass
  if (nq / nkv != kG || nkv != 2 || hd != kHd) return;
  __shared__ u16 sQ[16 * kHd];     // [16, 256] query rows (12..15 zero-padded)
  __shared__ u16 sK[2 * kChunk * kHd]; // double-buffered K rows (prefetch)
  __shared__ u16 sV[2 * kChunk * kHd]; // double-buffered V rows (prefetch)
  __shared__ float sS[16 * 16];    // [16, 16] scores (rows 12..15 unused)
  __shared__ u16 sP[kChunk * 16];  // [16 pos, 16] probs (cols 12..15 = 0)
  // sMax/sSum/sAlpha padded to 16 (PV lanes read [group+8] up to 15 for the
  // unused q-heads 12..15; padding avoids an OOB read).
  __shared__ float sMax[16];       // running softmax max per q-head
  __shared__ float sSum[16];       // running softmax denom per q-head
  __shared__ float sAlpha[16];     // this-chunk rescale factor per q-head
  const int t = blockIdx.x;
  const int kvh = blockIdx.y;
  const int warp_id = threadIdx.x >> 5;
  const int lane = threadIdx.x & 31;
  // B2 multi-seq: select this token's sequence KV + page_table slices.
  const u16* kv = d_seq_id
                      ? kv_cache + static_cast<size_t>(d_seq_id[t]) * kv_seq_stride
                      : kv_cache;
  const int* pt = d_seq_id
                      ? page_table + static_cast<size_t>(d_seq_id[t]) * pt_seq_stride
                      : page_table;
  const int* sel = topk + static_cast<size_t>(t) * max_topk;
  const int nsel_total = topk_len[t];
  const float scale = 1.f / sqrtf(static_cast<float>(kHd));
  // Stage 12 query rows into sQ; zero rows 12..15 (mma reads a 16-row tile).
  const int qh0 = kvh * kG;
  for (int i = threadIdx.x; i < 16 * kHd; i += 256) {
    const int m = i / kHd;
    sQ[i] = (m < kG)
                ? q[(static_cast<size_t>(t) * nq + qh0 + m) * kHd + (i % kHd)]
                : 0;
  }
  for (int i = threadIdx.x; i < kChunk * 16; i += 256) sP[i] = 0;
  if (threadIdx.x < 16) {
    sMax[threadIdx.x] = -1e30f;
    sSum[threadIdx.x] = 0.f;
    sAlpha[threadIdx.x] = 0.f;
  }
  __syncthreads();
  // PV output accumulator in REGISTERS (was shared sO with a per-chunk
  // read-modify-write over [12,256] floats — the dominant shared-memory
  // traffic). Warp w owns n-tiles {w, w+8, w+16, w+24}; each lane owns, per
  // n-tile, the 4 mma C values (2 q-heads x 2 dims). acc[j][0..3] mirrors the
  // mma C fragment (c0,c1 = q-head group; c2,c3 = q-head group+8).
  const int pv_group = lane >> 2;
  const int pv_col = (lane & 3) * 2;
  float acc[4][4];
  #pragma unroll
  for (int j = 0; j < 4; ++j) {
    acc[j][0] = 0.f;
    acc[j][1] = 0.f;
    acc[j][2] = 0.f;
    acc[j][3] = 0.f;
  }
  // KV gather is double-buffered: the next chunk's scattered paged-KV is
  // prefetched (cp.async) into the alternate buffer while this chunk's QK/PV
  // computes, hiding the LPDDR5x scatter latency that dominates this kernel.
  // The staged VALUES are identical to the old synchronous gather, so the
  // QK/softmax/PV math is bit-for-bit unchanged.
  auto stage = [&](int base_sel, int chunk, int buf) {
    u16* dK = sK + static_cast<size_t>(buf) * kChunk * kHd;
    u16* dV = sV + static_cast<size_t>(buf) * kChunk * kHd;
    // One 16-byte cp.async per (position, 8-dim group) for K and V: u in
    // [0,32) copies K dims [u*8, u*8+8); u in [32,64) copies the V dims.
    for (int i = threadIdx.x; i < chunk * 64; i += 256) {
      const int c = i >> 6;
      const int u = i & 63;
      const int is_v = u >> 5;
      const int d8 = (u & 31) * 8;
      const int p = sel[base_sel + c];
      const int sp = p < 0 ? 0 : p;
      const int slot = pt[sp] * kKvPageSize + (sp % kKvPageSize);
      const size_t g = (static_cast<size_t>(slot) * nkv + kvh) * (2 * kHd) +
                       (is_v ? kHd : 0) + d8;
      CpAsync16((is_v ? dV : dK) + c * kHd + d8, kv + g);
    }
    // Zero the [chunk, kChunk) tail (only the final partial chunk needs it);
    // finite so 0*x=0 in the PV mma (uninitialised shared could be a NaN).
    for (int i = threadIdx.x; i < (kChunk - chunk) * kHd; i += 256) {
      const int c = chunk + (i / kHd);
      const int d = i - (i / kHd) * kHd;
      dK[c * kHd + d] = 0;
      dV[c * kHd + d] = 0;
    }
    CpAsyncCommit();
  };
  int nsel = 0;
  int buf = 0;
  if (nsel_total > 0) stage(0, min(kChunk, nsel_total), 0);
  while (nsel < nsel_total) {
    const int chunk = min(kChunk, nsel_total - nsel);
    const int next_nsel = nsel + chunk;
    // Prefetch the next chunk into the alternate buffer (overlaps this chunk's
    // compute), then wait for THIS chunk's gather (issued last iteration /
    // prologue) to land. wait_group<1> keeps the next prefetch in flight.
    if (next_nsel < nsel_total) {
      stage(next_nsel, min(kChunk, nsel_total - next_nsel), buf ^ 1);
      CpAsyncWait<1>();
    } else {
      CpAsyncWait<0>();
    }
    __syncthreads();
    const u16* bK = sK + static_cast<size_t>(buf) * kChunk * kHd;
    const u16* bV = sV + static_cast<size_t>(buf) * kChunk * kHd;
    // ---- QK^T (warps 0-1, one n-tile each): sS[16,16] = sQ @ sK^T via mma. --
    // mma m16n8k16: A = sQ (m=qh, k=dim), B = sK^T (B[k][n]=sK[pos=n][dim=k]).
    // 16 k-tiles per n-tile; the 2 n-tiles (16 positions) run on warps 0 and 1
    // in parallel (was warp 0 serial). Bit-identical: disjoint output columns.
    if (warp_id < kChunk / 8) {
      const int nt = warp_id;
      const int nb = nt * 8;
      const int group = lane >> 2;     // qh row (0..7, and +8)
      const int k0 = (lane & 3) * 2;   // k-pair base
      const int col = (lane & 3) * 2;  // pos-pair base (C fragment)
      float c0 = 0.f, c1 = 0.f, c2 = 0.f, c3 = 0.f;
      for (int kt = 0; kt < kHd / 16; ++kt) {
        const int kb = kt * 16;
        const u16* qa = sQ + group * kHd + kb + k0;
        const u16* qa8 = sQ + (group + 8) * kHd + kb + k0;
        uint32_t a0 = *reinterpret_cast<const uint32_t*>(qa);
        uint32_t a1 = *reinterpret_cast<const uint32_t*>(qa8);
        uint32_t a2 = *reinterpret_cast<const uint32_t*>(qa + 8);
        uint32_t a3 = *reinterpret_cast<const uint32_t*>(qa8 + 8);
        const u16* kbp = bK + (nb + group) * kHd + kb + k0;
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
    // ---- Online softmax per q-head row (thread q handles qh=q). ----
    if (threadIdx.x < kG) {
      const int qrow = threadIdx.x;
      const float m_old = sMax[qrow];
      float mx = m_old;
      for (int p = 0; p < chunk; ++p)
        mx = fmaxf(mx, sS[qrow * 16 + p] * scale);
      const float alpha = expf(m_old - mx);
      sAlpha[qrow] = alpha;
      float s = sSum[qrow] * alpha;
      for (int p = 0; p < kChunk; ++p) {
        const float pr = (p < chunk) ? expf(sS[qrow * 16 + p] * scale - mx)
                                     : 0.f;
        sP[p * 16 + qrow] = FloatToBf16(pr);
        s += pr;
      }
      sSum[qrow] = s;
      sMax[qrow] = mx;
    }
    __syncthreads();
    // ---- PV: acc += sP^T[16,16] @ sV[16,256] via mma (register accum). ----
    // out[qh][d] = sum_pos P[pos][qh] * V[pos][d].
    // mma: A = sP^T (m=qh, k=pos): A[qh][pos] = sP[pos*16 + qh] (stride 16).
    //      B = sV (k=pos, n=d): B[pos][d] = sV[pos*hd + d] (stride hd in k).
    // Warp w handles n-tiles w, w+8, w+16, w+24 (disjoint 8-dim slices); the
    // running accumulator lives in registers (acc[j]) — no shared sO traffic.
    {
      const int k0 = (lane & 3) * 2;   // pos-pair base (mma K)
      const float ag = sAlpha[pv_group];
      const float ag8 = sAlpha[pv_group + 8];
      // A-fragment is the same for every n-tile (depends only on q-heads/pos).
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
      for (int j = 0; j < 4; ++j) {
        const int nt = warp_id + j * 8;
        const int nb = nt * 8;
        // B-fragment: elements strided by hd in sV -> scalar load + pack.
        const u16* vb = bV + nb + pv_group;
        uint32_t b0 = static_cast<uint32_t>(vb[k0 * kHd]) |
                      (static_cast<uint32_t>(vb[(k0 + 1) * kHd]) << 16);
        uint32_t b1 = static_cast<uint32_t>(vb[(k0 + 8) * kHd]) |
                      (static_cast<uint32_t>(vb[(k0 + 9) * kHd]) << 16);
        float c0 = 0.f, c1 = 0.f, c2 = 0.f, c3 = 0.f;
        MmaBf16(c0, c1, c2, c3, a0, a1, a2, a3, b0, b1);
        // Fold the per-chunk online-softmax rescale into the register accum.
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
  // ---- Normalize + fused gate + write out (from register accumulators). ----
  // Lane (warp w, lane L) owns q-heads group=L/4 and group+8, dims nb+col,
  // nb+col+1 for n-tiles nt = w + j*8 (j=0..3).
  {
    const float lg = sSum[pv_group];
    const float lg8 = sSum[pv_group + 8];
    const int qh_a = qh0 + pv_group;
    const int qh_b = qh0 + pv_group + 8;
    #pragma unroll
    for (int j = 0; j < 4; ++j) {
      const int nb = (warp_id + j * 8) * 8;
      const int d = nb + pv_col;
      // q-head group (always valid, 0..7 < 12).
      {
        const size_t o0 = (static_cast<size_t>(t) * nq + qh_a) * kHd + d;
        float v0 = (lg > 0.f) ? acc[j][0] / lg : acc[j][0];
        float v1 = (lg > 0.f) ? acc[j][1] / lg : acc[j][1];
        const float g0 = 1.f / (1.f + expf(-Bf16ToFloat(gate[o0])));
        const float g1 = 1.f / (1.f + expf(-Bf16ToFloat(gate[o0 + 1])));
        out[o0] = FloatToBf16(Bf16ToFloat(FloatToBf16(v0)) * g0);
        out[o0 + 1] = FloatToBf16(Bf16ToFloat(FloatToBf16(v1)) * g1);
      }
      // q-head group+8 (valid only for group 0..3 -> qh 8..11).
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

void FullAttentionWeights::Free() {
  auto freep = [](void* p) { if (p) cudaFree(p); };
  freep(q_proj); freep(k_proj); freep(v_proj); freep(o_proj);
  freep(q_norm); freep(k_norm);
  freep(index_qk_proj); freep(index_q_norm); freep(index_k_norm);
  q_proj_fp8.Free(); k_proj_fp8.Free(); v_proj_fp8.Free(); o_proj_fp8.Free();
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

  // FP8 (e4m3) decode shadows for the large projections (gated by
  // Q4T_FP8_PROJ; a no-op returning empty shadows when off). The prior loads
  // use pageable H2D (synchronous), so the weights are already resident when
  // the quantize kernels enqueue on `stream`.
  if (!BuildFp8Shadow(out->q_proj, nq * 2 * hd, hidden_size, &out->q_proj_fp8,
                      Fp8Part::kAttn, stream))
    return Status::Fail("q_proj FP8 shadow");
  if (!BuildFp8Shadow(out->k_proj, nkv * hd, hidden_size, &out->k_proj_fp8,
                      Fp8Part::kAttn, stream))
    return Status::Fail("k_proj FP8 shadow");
  if (!BuildFp8Shadow(out->v_proj, nkv * hd, hidden_size, &out->v_proj_fp8,
                      Fp8Part::kAttn, stream))
    return Status::Fail("v_proj FP8 shadow");
  if (!BuildFp8Shadow(out->o_proj, hidden_size, nq * hd, &out->o_proj_fp8,
                      Fp8Part::kAttn, stream))
    return Status::Fail("o_proj FP8 shadow");
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
  const int max_topk = 2052;    // capacity; at most 2048 + 3 valid tokens
  size_t off = 0;
  auto alloc = [&](size_t bytes) { off += (bytes + 255) & ~size_t(255); };
  alloc(static_cast<size_t>(T) * qg_dim * 2);  // d_qg
  alloc(static_cast<size_t>(T) * kv_dim * 2);  // d_k
  alloc(static_cast<size_t>(T) * kv_dim * 2);  // d_v
  alloc(static_cast<size_t>(T) * nq * hd * 2);  // d_q
  alloc(static_cast<size_t>(T) * nq * hd * 2);  // d_gate
  alloc(static_cast<size_t>(T) * n_iq * idx_hd * 2);  // d_iq
  alloc(static_cast<size_t>(T) * n_ik * idx_hd * 2);  // d_ik
  alloc(static_cast<size_t>(T) * max_blocks * 4);     // d_logits (f32)
  alloc(static_cast<size_t>(T) * max_topk * 4);       // d_topk (i32)
  alloc(static_cast<size_t>(T) * 4);                  // d_topk_len (i32)
  alloc(static_cast<size_t>(T) * nq * hd * 2);        // d_attn
  alloc(static_cast<size_t>(T) * n_iq * max_blocks * 2);  // d_S (indexer GEMM)
  const int block_topk = 512;  // idx_budget / compress (streaming top-k)
  alloc(static_cast<size_t>(T) * block_topk * 4);  // d_run_val (f32)
  alloc(static_cast<size_t>(T) * block_topk * 4);  // d_run_idx (i32)
  // One-pass long-context indexer buffers (decode / small T only).
  if (T <= kOnePassT) {
    const int n_groups_ws = std::max(1, w.max_len / w.idx_compress);
    alloc(static_cast<size_t>(T) * n_groups_ws * 4);  // one-pass logits [T,groups]
    alloc(static_cast<size_t>(T) * kMaxOnePassCand * 4);  // cand val 0
    alloc(static_cast<size_t>(T) * kMaxOnePassCand * 4);  // cand idx 0
    alloc(static_cast<size_t>(T) * kMaxOnePassCand * 4);  // cand val 1
    alloc(static_cast<size_t>(T) * kMaxOnePassCand * 4);  // cand idx 1
  }
  off += 32u * 1024u * 1024u;  // GEMM scratch
  return off;
}

Status FullAttentionForward(const FullAttentionWeights& w, const uint16_t* x,
                            uint16_t* out, const int* positions,
                            const int* rope_pos,
                            uint16_t* kv_cache, const int* page_table,
                            uint16_t* idx_raw, uint16_t* idx_comp, int T,
                            void* workspace, size_t workspace_bytes,
                            cudaStream_t stream, const int* d_seq_id,
                            int max_position) {
  if (T <= 0 || T > kMaxT)
    return Status::Fail("FullAttentionForward: T out of range [1, " +
                        std::to_string(kMaxT) + "]");
  if (max_position < -1 || max_position >= w.max_len)
    return Status::Fail("FullAttentionForward: max_position out of range");
  const int hs = w.hidden_size;
  const int nq = w.nq, nkv = w.nkv, hd = w.hd;
  const int qg_dim = nq * 2 * hd;
  const int kv_dim = nkv * hd;
  const int idx_hd = w.idx_head_dim;
  const int n_iq = w.idx_n_heads, n_ik = w.idx_kv_heads;
  // Cap to one sequence's idx_comp slice (max_len rows). The single-seq
  // indexer GEMM reads idx_comp as [max_blocks, idx_hd]; a fixed kMaxBlocks
  // (2048) overruns the seq-0 slice when max_seq*max_len < 2048 (e.g. serve
  // --max-seq 2 --max-len 640 -> illegal memory access). Visible blocks are
  // always <= max_len/compress < max_len, so this cap never drops a real one.
  const int max_blocks = std::min(kMaxBlocks, w.max_len);
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
  f32* d_logits = static_cast<f32*>(alloc(static_cast<size_t>(T) * max_blocks * 4));
  int* d_topk = static_cast<int*>(alloc(static_cast<size_t>(T) * max_topk * 4));
  int* d_topk_len = static_cast<int*>(alloc(static_cast<size_t>(T) * 4));
  u16* d_attn = static_cast<u16*>(alloc(static_cast<size_t>(T) * nq * hd * 2));
  u16* d_S = static_cast<u16*>(
      alloc(static_cast<size_t>(T) * n_iq * max_blocks * 2));  // indexer GEMM out
  // Streaming top-k running buffers (long context): per-query top-block_topk
  // scores + global block ids, merged across CHUNK slices.
  const int stream_block_topk = 512;  // idx_budget / compress
  f32* d_run_val =
      static_cast<f32*>(alloc(static_cast<size_t>(T) * stream_block_topk * 4));
  int* d_run_idx =
      static_cast<int*>(alloc(static_cast<size_t>(T) * stream_block_topk * 4));
  // One-pass long-context indexer buffers (decode / small T only).
  f32* d_op_logits = nullptr;
  f32* d_cand_val0 = nullptr;
  int* d_cand_idx0 = nullptr;
  f32* d_cand_val1 = nullptr;
  int* d_cand_idx1 = nullptr;
  if (T <= kOnePassT) {
    const int n_groups_ws = std::max(1, w.max_len / w.idx_compress);
    d_op_logits =
        static_cast<f32*>(alloc(static_cast<size_t>(T) * n_groups_ws * 4));
    d_cand_val0 =
        static_cast<f32*>(alloc(static_cast<size_t>(T) * kMaxOnePassCand * 4));
    d_cand_idx0 =
        static_cast<int*>(alloc(static_cast<size_t>(T) * kMaxOnePassCand * 4));
    d_cand_val1 =
        static_cast<f32*>(alloc(static_cast<size_t>(T) * kMaxOnePassCand * 4));
    d_cand_idx1 =
        static_cast<int*>(alloc(static_cast<size_t>(T) * kMaxOnePassCand * 4));
  }
  if (off > workspace_bytes)
    return Status::Fail("FullAttentionForward: workspace too small (need " +
                        std::to_string(off) + ", have " +
                            std::to_string(workspace_bytes) + ")");

  const size_t gemm_ws = 32u * 1024u * 1024u;
  void* d_gemm_ws = static_cast<char*>(workspace) + off;
  off += gemm_ws;
  if (off > workspace_bytes)
    return Status::Fail("FullAttentionForward: workspace too small for GEMM");

  // `positions` is a DEVICE int[T] (absolute token positions), the same
  // contract as `rope_pos`. It is read directly by the kernels below; no
  // host->device copy is performed here. (An earlier version treated it as a
  // host pointer and did a cudaMemcpyHostToDevice, which faulted when the
  // model layer passed its persistent device buffer `m.d_positions`.)
  const int* d_positions = positions;

  // B2 multi-seq per-sequence strides (elements), used by the kernels to
  // select the per-token state slice via d_seq_id. Only meaningful when
  // d_seq_id != null (the pooled bases are passed in that case).
  const size_t n_pages = (w.max_len + kKvPageSize - 1) / kKvPageSize;
  const size_t kv_seq_stride =
      n_pages * kKvPageSize * nkv * 2 * hd;  // uint16 (one seq's paged KV)
  const int pt_seq_stride = w.max_len;  // int (page_table [max_seq, max_len])
  const size_t idx_seq_stride =
      static_cast<size_t>(w.max_len) * idx_hd;  // uint16 (idx [max_seq, max_len, idx_hd])
  const int rope_seq_stride = 3 * w.max_len;  // int (rope [max_seq, 3, max_len])

  Status s;
  // 1. qg = x @ W_q^T ; k = x @ W_k^T ; v = x @ W_v^T
  s = CheckGemm(ProjGemm(x, w.q_proj, &w.q_proj_fp8, d_qg, T, qg_dim, hs, 1.f,
                         0.f, d_gemm_ws, gemm_ws, stream));
  if (!s.ok()) return s;
  s = CheckGemm(ProjGemm(x, w.k_proj, &w.k_proj_fp8, d_k, T, kv_dim, hs, 1.f,
                         0.f, d_gemm_ws, gemm_ws, stream));
  if (!s.ok()) return s;
  s = CheckGemm(ProjGemm(x, w.v_proj, &w.v_proj_fp8, d_v, T, kv_dim, hs, 1.f,
                         0.f, d_gemm_ws, gemm_ws, stream));
  if (!s.ok()) return s;

  // 2. Fused q/k preprocessing: deinterleave qg -> q + gate + centered
  //    RMSNorm(q) AND centered RMSNorm(k) in one launch (disjoint heads).
  QKDeinterleaveNormKernel<<<T * (nq + nkv), hd, 0, stream>>>(
      d_qg, w.q_norm, d_q, d_gate, nq, d_k, w.k_norm, T, nkv, hd, w.eps);
  // 3. partial RoPE on q (nq heads) and k (nkv heads), first rot_d dims
  //    (3D MRoPE: rope_pos [3, max_len], row chosen by i % 3).
  const int half = w.rot_d / 2;
  PartialRopeKernel<24><<<(T * nq * half + 255) / 256, 256, 0, stream>>>(
      d_q, nq, hd, w.rot_d, d_positions, rope_pos, w.max_len, w.rope_theta, T,
      d_seq_id, rope_seq_stride);
  PartialRopeKernel<2><<<(T * nkv * half + 255) / 256, 256, 0, stream>>>(
      d_k, nkv, hd, w.rot_d, d_positions, rope_pos, w.max_len, w.rope_theta, T,
      d_seq_id, rope_seq_stride);
  // 5. write k, v into the paged KV cache
  WriteKVKernel<<<(T * nkv * hd + 255) / 256, 256, 0, stream>>>(
      d_k, d_v, kv_cache, nkv, hd, d_positions, page_table, T, d_seq_id,
      kv_seq_stride, pt_seq_stride);
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
  // 7. Normalize and rotate queries only. Keys remain the raw projection
  //    until BuildCompressedKKernel pools and transforms each complete group.
  IndexerQueryNormRopeKernel<<<T * n_iq, idx_hd, 0, stream>>>(
      d_iq, w.index_q_norm, d_positions, rope_pos, w.max_len, n_iq, idx_hd,
      w.rot_d, w.rope_theta, w.eps, d_seq_id, rope_seq_stride);
  // 8a. Store the raw projection directly; no transformed key copy is needed.
  WriteIndexRawKernel<<<T, idx_hd, 0, stream>>>(
      d_ik, idx_raw, d_positions, T, idx_hd, d_seq_id, idx_seq_stride);
  // 8b. build compressed keys for groups completed in this batch. Runs in a
  // separate launch so the kernel-boundary sync makes every idx_raw write
  // from 8a visible before the group averages read them (no prefill race).
  BuildCompressedKKernel<<<T, idx_hd, 0, stream>>>(
      w.index_k_norm, idx_raw, idx_comp, d_positions, rope_pos, T,
      w.max_len, idx_hd, w.idx_compress, w.rope_theta, w.eps, d_seq_id,
      idx_seq_stride, rope_seq_stride);
  // 9-10. Block scoring + top-k selection.
  //
  // Short/medium context (max visible groups <= kMaxBlocks, i.e. <= 8192
  // tokens): score all blocks into one [T, max_blocks] buffer and bitonic-
  // select in a single shot (unchanged fast path).
  //
  // Long context (> 8192 tokens): the candidate set spans more than kMaxBlocks
  // compressed blocks, which cannot be materialised as [T, n_groups] for
  // prefill. Stream the score over CHUNK = max_blocks slices and merge each
  // chunk's local top-k into a running per-query top-block_topk (see the
  // streaming kernels above). This restores long-context recall: without it
  // only the first 2048 blocks (first 8192 tokens) were ever candidates.
  const int block_topk = w.idx_block_topk();
  // Model callers provide the exact host-side maximum of the uploaded
  // logical positions. Device-only callers (e.g. MTP draft) keep readback.
  int max_pos = max_position;
  if (max_pos < 0) {
    max_pos = 0;
    std::vector<int> hp(static_cast<size_t>(T));
    if (cudaMemcpyAsync(hp.data(), d_positions,
                        static_cast<size_t>(T) * sizeof(int),
                        cudaMemcpyDeviceToHost, stream) != cudaSuccess)
      return Status::Fail("cudaMemcpyAsync positions failed");
    if (cudaStreamSynchronize(stream) != cudaSuccess)
      return Status::Fail("cudaStreamSynchronize positions failed");
    for (int i = 0; i < T; ++i) max_pos = std::max(max_pos, hp[i]);
  }
  const int n_groups_max = (max_pos + 1) / w.idx_compress;
  if (n_groups_max <= max_blocks) {
    // Single-shot fast path (context <= kMaxBlocks * compress). Single-seq:
    // tensor-core GEMM S = iq @ ck^T + relu/sum/mask reduce. Multi-seq
    // (d_seq_id): per-sequence idx_comp slices can't share one GEMM weight, so
    // keep the SIMT kernel.
    if (d_seq_id == nullptr) {
      s = CheckGemm(Bf16Gemm(d_iq, idx_comp, d_S, T * n_iq, max_blocks, idx_hd,
                             1.f, 0.f, d_gemm_ws, gemm_ws, stream));
      if (!s.ok()) return s;
      const int total = T * max_blocks;
      IndexerReduceKernel<<<(total + 255) / 256, 256, 0, stream>>>(
          d_S, d_logits, d_positions, T, n_iq, idx_hd, w.idx_compress,
          max_blocks, /*block_off=*/0);
    } else {
      IndexerLogitsKernel<<<T, 256, 0, stream>>>(
          d_iq, idx_comp, d_logits, d_positions, T, n_iq, idx_hd,
          w.idx_compress, max_blocks, /*block_off=*/0, d_seq_id,
          idx_seq_stride);
    }
    TopkSelectKernel<<<T, 256, 0, stream>>>(d_logits, d_topk, d_topk_len,
                                            d_positions, T, w.idx_compress,
                                            block_topk, max_blocks, max_topk);
  } else if (T <= kOnePassT && n_iq * idx_hd <= 512) {
    // One-pass long-context indexer (decode / MTP verify, T <= kOnePassT).
    // The [T, n_groups] logits buffer is small enough to materialise, so score
    // ALL blocks in one pass (warp-per-block, coalesced) and select the global
    // top-block_topk with a small multi-level parallel reduction. This removes
    // the O(n_chunks) streaming-merge cost (6 chunks x 12 layers x 132us ~=
    // 9.5ms/step at 30K) and the under-parallelised chunked scoring. The
    // selected set is the exact global top-block_topk (lemma in the kernel
    // header). Short context (<= max_blocks) and prefill (large T) are
    // unchanged.
    if (block_topk > kMaxBlockTopk)
      return Status::Fail(
          "FullAttentionForward: block_topk exceeds kMaxBlockTopk "
          "(one-pass top-k shared buffer)");
    OnePassScoreKernel<<<dim3((n_groups_max + 7) / 8, T), 256, 0, stream>>>(
        d_iq, idx_comp, d_op_logits, d_positions, T, n_iq, idx_hd,
        w.idx_compress, n_groups_max, d_seq_id, idx_seq_stride);
    // Multi-level reduction: each level takes the local top-block_topk of every
    // 2048-window of the current candidate list, converging to <= max_blocks,
    // then one final sort. Ping-pong two candidate buffers.
    const int n_slices0 = (n_groups_max + max_blocks - 1) / max_blocks;
    int cur = n_slices0 * block_topk;  // candidates after level 0
    SliceLocalTopkKernel<<<dim3(n_slices0, T), 256, 0, stream>>>(
        d_op_logits, n_groups_max, max_blocks, block_topk, T, d_cand_val0,
        d_cand_idx0, cur);
    f32* in_v = d_cand_val0;
    int* in_i = d_cand_idx0;
    int in_stride = cur;
    f32* out_v = d_cand_val1;
    int* out_i = d_cand_idx1;
    while (cur > max_blocks) {
      const int n_slices = (cur + max_blocks - 1) / max_blocks;
      const int next = n_slices * block_topk;
      WindowMergeTopkKernel<<<dim3(n_slices, T), 256, 0, stream>>>(
          in_v, in_i, cur, in_stride, max_blocks, block_topk, T, out_v, out_i,
          next);
      f32* tv = in_v;
      in_v = out_v;
      out_v = tv;
      int* ti = in_i;
      in_i = out_i;
      out_i = ti;
      in_stride = next;
      cur = next;
    }
    FinalTopkExpandKernel<<<T, 256, 0, stream>>>(
        in_v, in_i, cur, in_stride, d_positions, T, w.idx_compress, block_topk,
        d_topk, d_topk_len, max_topk);
  } else {
    // Streaming path (long context, prefill large T). Score blocks in
    // CHUNK = max_blocks slices and keep a running per-query top-block_topk
    // merged across chunks.
    if (block_topk > kMaxBlockTopk)
      return Status::Fail(
          "FullAttentionForward: block_topk exceeds kMaxBlockTopk "
          "(streaming top-k shared buffer)");
    InitRunTopkKernel<<<(T * block_topk + 255) / 256, 256, 0, stream>>>(
        d_run_val, d_run_idx, T * block_topk);
    const int num_chunks = (n_groups_max + max_blocks - 1) / max_blocks;
    // Small T (decode / MTP verify, T <= 4): block-parallel streaming kernels
    // (grid over block tiles) — the token-parallel kernels above launch one
    // block per token, which is a single block for T=1 and leaves 19/20 SMs
    // idle. Valid with OR without d_seq_id (T <= 4: d_seq_id[t] is fixed per
    // token, so the per-seq idx_comp slice is selected once per block). Large
    // T (prefill) keeps the tensor-core GEMM scoring path (Bf16Gemm +
    // IndexerReduce) which saturates the SMs via the GEMM.
    const bool block_par = T <= 4 && n_iq * idx_hd <= 512;
    for (int c = 0; c < num_chunks; ++c) {
      const int block_off = c * max_blocks;
      if (block_par) {
        IndexerLogitsBlockParKernel<<<dim3(max_blocks / 256, T), 256, 0,
                                       stream>>>(
            d_iq, idx_comp + static_cast<size_t>(block_off) * idx_hd, d_logits,
            d_positions, T, n_iq, idx_hd, w.idx_compress, max_blocks,
            block_off, d_seq_id, idx_seq_stride);
        // Merge stays token-parallel (one block per token): it is correct and
        // cheap (~137us at T=1); only the scoring was the under-parallelized
        // dominant cost. (A block-parallel merge would race on run_val.)
        MergeChunkTopkKernel<<<T, 256, 0, stream>>>(
            d_logits, block_off, max_blocks, block_topk, T, d_run_val,
            d_run_idx);
      } else if (d_seq_id == nullptr) {
        s = CheckGemm(Bf16Gemm(
            d_iq, idx_comp + static_cast<size_t>(block_off) * idx_hd, d_S,
            T * n_iq, max_blocks, idx_hd, 1.f, 0.f, d_gemm_ws, gemm_ws,
            stream));
        if (!s.ok()) return s;
        const int total = T * max_blocks;
        IndexerReduceKernel<<<(total + 255) / 256, 256, 0, stream>>>(
            d_S, d_logits, d_positions, T, n_iq, idx_hd, w.idx_compress,
            max_blocks, block_off);
        MergeChunkTopkKernel<<<T, 256, 0, stream>>>(
            d_logits, block_off, max_blocks, block_topk, T, d_run_val,
            d_run_idx);
      } else {
        IndexerLogitsKernel<<<T, 256, 0, stream>>>(
            d_iq, idx_comp, d_logits, d_positions, T, n_iq, idx_hd,
            w.idx_compress, max_blocks, block_off, d_seq_id, idx_seq_stride);
        MergeChunkTopkKernel<<<T, 256, 0, stream>>>(
            d_logits, block_off, max_blocks, block_topk, T, d_run_val,
            d_run_idx);
      }
    }
    ExpandRunTopkKernel<<<T, 256, 0, stream>>>(
        d_run_idx, d_run_val, d_positions, T, w.idx_compress, block_topk,
        d_topk, d_topk_len, max_topk);
  }
  // 11. sparse GQA attention over the selected positions (valid only, via
  //     topk_len — the -1 tail is not iterated). The sigmoid gate is applied
  //     in-kernel (fused), so no separate GateMulKernel pass is needed.
  // Tensor-core GQA-packed: grid (T, nkv), each block reads its KV chunk once
  // and feeds all 12 q-heads via mma.sync (12x KV read reduction vs the old
  // per-q-head SIMT kernel).
  SparseAttentionKernel<<<dim3(T, nkv), 256, 0, stream>>>(
      d_q, kv_cache, page_table, d_topk, d_topk_len, d_attn, d_gate, nq, nkv,
      hd, max_topk, d_seq_id, kv_seq_stride, pt_seq_stride);
  // 12. out = attn @ W_o^T
  s = CheckGemm(ProjGemm(d_attn, w.o_proj, &w.o_proj_fp8, out, T, hs, nq * hd,
                         1.f, 0.f, d_gemm_ws, gemm_ws, stream));
  return s;
}

}  // namespace model
}  // namespace q4t
