// Full attention (QSA sparse attention) — the qwen4_exp "full_attention"
// decoder layer's attention block (12 layers, every 4th).
//
// qwen4_exp full attention = GQA (24 q heads / 2 kv heads, head_dim 256) with
// partial MRoPE (rotary_dim 64 = 0.25 * 256, theta 1e7) + an attention output
// gate, PLUS a Qwen Sparse Attention (QSA) indexer that selects a sparse set
// of key positions per query instead of attending to the whole prefix.
//
// Main attention (per token, causal):
//   qg = x @ W_q^T   [T, 24*2*256]  (Q and Gate interleaved per head)
//   k  = x @ W_k^T   [T, 2*256]
//   v  = x @ W_v^T   [T, 2*256]
//   deinterleave qg -> q [T,24,256], gate [T,24,256]
//   q = per-head RMSNorm(q, centered) ; k = per-head RMSNorm(k, centered)
//   q, k = partial MRoPE (first 64 dims)
//   write k, v to the per-layer KV cache
//   topk = QSA indexer (below)
//   attn = sparse GQA attention over the selected token positions
//   attn = attn * sigmoid(gate)
//   out  = attn @ W_o^T   [T, hs]
//
// QSA indexer (compressed variant, compress_ratio 4, budget 2048, block_topk
// 512): a tiny 4-query/1-key MQA head (head_dim 128) that scores *compressed*
// keys (groups of 4 tokens, average-pooled) and picks the top-512 groups,
// expanded to 2048 token positions (+ the current group's tail tokens).
//   iq, ik = x @ W_index_qk^T  -> iq [T,4,128], ik [T,1,128]
//   iq = GemmaRMSNorm(iq) ; ik = GemmaRMSNorm(ik)   (plain, not centered)
//   iq, ik = partial MRoPE (first 64 dims)
//   raw ik stored per token; each completed group of 4 is average-pooled,
//   GemmaRMSNorm'd and MRoPE'd (at the group's first position) -> compressed K
//   logits[t, g] = sum_h relu(iq[t,h] . ck[g]) / sqrt(128)   (g < (pos+1)/4)
//   topk = top-512 blocks -> expand to 2048 token indices + tail
//
// NOTE: the engine is text-only, so MRoPE's three position axes are all the
// same scalar position; partial MRoPE reduces to standard RoPE on the first
// 64 dims.
#pragma once

#include <cuda_runtime.h>

#include <cstdint>
#include <string>

#include "q4t/io/weight_loader.h"
#include "q4t/status.h"

namespace q4t {
namespace model {

// Device weights of one full_attention (QSA) block. BF16 (uint16) row-major.
struct FullAttentionWeights {
  int hidden_size = 2560;
  int nq = 24;  // num_attention_heads
  int nkv = 2;  // num_key_value_heads
  int hd = 256;  // head_dim
  int rot_d = 64;  // partial rotary dim (0.25 * hd)
  float rope_theta = 1e7f;
  float eps = 1e-6f;
  // QSA indexer
  int idx_n_heads = 4;
  int idx_kv_heads = 1;
  int idx_head_dim = 128;
  int idx_budget = 2048;  // token_topk
  int idx_compress = 4;  // compress_ratio
  int idx_block_topk() const { return idx_budget / idx_compress; }  // 512

  uint16_t* q_proj = nullptr;  // [nq*2*hd, hs] (Q+Gate interleaved)
  uint16_t* k_proj = nullptr;  // [nkv*hd, hs]
  uint16_t* v_proj = nullptr;  // [nkv*hd, hs]
  uint16_t* o_proj = nullptr;  // [hs, nq*hd]
  uint16_t* q_norm = nullptr;  // [hd] (centered)
  uint16_t* k_norm = nullptr;  // [hd] (centered)
  // QSA indexer
  uint16_t* index_qk_proj = nullptr;  // [(idx_n_heads+idx_kv_heads)*idx_head_dim, hs]
  uint16_t* index_q_norm = nullptr;  // [idx_head_dim] (plain)
  uint16_t* index_k_norm = nullptr;  // [idx_head_dim] (plain)

  void Free();
};

// Load one full_attention block's weights from `loader` under
//   {prefix}.q_proj.weight / k_proj.weight / v_proj.weight / o_proj.weight
//   {prefix}.q_norm.weight / k_norm.weight
//   {prefix}.indexer.index_qk_proj.weight
//   {prefix}.indexer.q_layernorm.weight / k_layernorm.weight
// e.g. prefix = "model.language_model.layers.3.self_attn".
Status LoadFullAttention(const io::WeightLoader& loader, const std::string& prefix,
                         int hidden_size, int nq, int nkv, int hd, int rot_d,
                         float rope_theta, float eps, int idx_n_heads,
                         int idx_kv_heads, int idx_head_dim, int idx_budget,
                         int idx_compress, FullAttentionWeights* out,
                         cudaStream_t stream);

// Run one full_attention (QSA) block forward for a single sequence (prefill).
//
//   x        : device row-major [T, hs] uint16 (BF16)
//   out      : device row-major [T, hs] uint16 (out)
//   positions: host int [T] absolute token positions (single sequence, so
//              positions[i] = seq_start + i)
//   kv_cache : device [max_len, nkv, hd] uint16 (K and V interleaved per
//              position: kv[pos*2*hd + 0*hd] = K, +hd = V), persistent
//   idx_raw  : device [max_len, idx_head_dim] uint16 — raw (pre-RoPE) index
//              keys, persistent
//   idx_comp : device [max_len, idx_head_dim] uint16 — compressed keys (one
//              per group of idx_compress tokens), persistent
//   T        : number of tokens in this chunk (== sequence length for a
//              single prefill)
//   workspace: scratch device buffer (>= ~128 MiB) for projections + logits +
//              topk
Status FullAttentionForward(const FullAttentionWeights& w, const uint16_t* x,
                            uint16_t* out, const int* positions,
                            uint16_t* kv_cache, uint16_t* idx_raw,
                            uint16_t* idx_comp, int T, void* workspace,
                            size_t workspace_bytes, cudaStream_t stream);

}  // namespace model
}  // namespace q4t
