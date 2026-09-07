// MTP (Multi-Token Predictor) draft model — 1 full-attention decoder layer.
//
// The MTP draft model (reference/vllm/vllm/models/qwen4_exp/nvidia/mtp.py)
// reuses the main model's embedding + lm_head (mtp_use_dedicated_embeddings
// = false) and adds:
//   - fc_embedding  [H, H]  + pre_fc_norm_embedding (GemmaRMSNorm)
//   - fc_hidden     [H, H]  + pre_fc_norm_hidden    (GroupedGemmaRMSNorm,
//                                                    over the hc*H multi stream)
//   - 1 full-attention decoder layer (3 HyperConnections + full attention +
//     BF16 MoE)
//   - hyper_connection_mixer (GatedResidual, use_combine=false)
//
// Forward (one draft step), scheme A:
//   emb        = embed_tokens(input_ids)                 [T, H]
//   prev_block = fc_embedding(pre_fc_norm_embedding(emb)) [T, H]
//   hs_multi   = pre_fc_norm_hidden(hidden_states)        [T, hc*H]
//   hs_multi   = flatten(fc_hidden(hs_multi.view(T,hc,H))) [T, hc*H]
//   (trunk, mlp_out, mlp_inj) = MtpDecoderLayer(hs_multi, prev_block, positions)
//   multi_hidden   = trunk + mlp_out                      [T, hc*H]  (unit-weight
//                                                            combine, no gate)
//   sample_hidden  = mixer.mix(multi_hidden)              [T, H]
//   logits         = lm_head(sample_hidden)               [T, vocab]
//
// The MTP decoder layer uses the SAME immediate-combine HC orchestration as
// the main model's full-attention layer (attn_hc.mix -> self_attn ->
// attn_hc.combine -> mlp_hc.mix -> MoE), but STOPS before the final mlp_hc
// combine: it returns (combined_attn_trunk, mlp_out, mlp_injection) so the
// external final mixer can do the unit-weight combine (the main model's
// combine is always a learned-injection combine, but the MTP final mixer's
// first combine has prev_injection=None -> unit weight).
#pragma once

#include <cuda_runtime.h>

#include <cstdint>
#include <string>

#include "q4t/io/weight_loader.h"
#include "q4t/model/full_attention.h"
#include "q4t/model/hyperconnection.h"
#include "q4t/model/model.h"
#include "q4t/model/moe.h"
#include "q4t/mtp/moe_bf16.h"
#include "q4t/status.h"

namespace q4t {
namespace mtp {

// Static configuration of the MTP draft model.
struct MtpConfig {
  std::string mtp_dir;  // dir with model.safetensors.index.json + shards
  int hs = 2560;
  int hc = 4;
  int lowrank = 320;
  int E = 512;
  int moe_is = 640;
  int shared_is = 640;
  int topk = 10;
  int vocab = 248320;
  int max_len = 8192;  // MTP full-attention KV/indexer length
  int max_prefill = 2048;  // sizes the forward workspace
  float eps = 1e-6f;
  // full-attention dims (same as the main model).
  int nq = 24;
  int nkv = 2;
  int hd = 256;
  int rot_d = 64;
  float rope_theta = 1e7f;
  int idx_n_heads = 4;
  int idx_kv_heads = 1;
  int idx_head_dim = 128;
  int idx_budget = 2048;
  int idx_compress = 4;
};

// The MTP draft model: fc projections + 1 full-attention decoder layer +
// final mixer. The embedding + lm_head are BORROWED from the main model (not
// owned here).
struct MtpModel {
  MtpConfig cfg;
  // Borrowed from the main model (must outlive this MtpModel).
  const uint16_t* embed_tokens = nullptr;  // [vocab, hs]
  const uint16_t* lm_head = nullptr;  // [vocab, hs]

  // MTP-specific weights.
  uint16_t* fc_embedding = nullptr;  // [hs, hs]
  uint16_t* fc_hidden = nullptr;  // [hs, hs]
  uint16_t* pre_fc_norm_embedding = nullptr;  // [hs]
  uint16_t* pre_fc_norm_hidden = nullptr;  // [hc*hs]
  model::HyperConnectionWeights attn_hc;  // use_combine=true
  model::HyperConnectionWeights mlp_hc;  // use_combine=true
  model::FullAttentionWeights full_attn;
  MoeBf16Weights moe;  // 512 routed BF16 experts
  model::MoEExtraWeights moe_extra;  // router + shared expert (BF16)
  model::HyperConnectionWeights mixer;  // use_combine=false (no block_inject)

  // Persistent device buffers (allocated in LoadMtp, freed in Free).
  uint16_t* kv_cache = nullptr;  // [n_pages, kKvPageSize, nkv, 2, hd]
  int* page_table = nullptr;  // [max_len] identity mapping
  uint16_t* idx_raw = nullptr;  // [max_len, idx_head_dim]
  uint16_t* idx_comp = nullptr;  // [max_len, idx_head_dim]
  void* d_ws = nullptr;  // forward workspace (GEMM ws + scratch)
  size_t ws_bytes = 0;
  size_t kv_bytes = 0;  // bytes of kv_cache (for ResetState)

  // Speculative-step scratch (allocated in LoadMtp, sized for one draft step
  // of a single token): the sample_hidden [hs], the multi_hidden trunk [hc*hs],
  // and the logits [vocab].
  uint16_t* d_sample = nullptr;
  uint16_t* d_trunk = nullptr;
  uint16_t* d_logits = nullptr;

  int hc_dim() const { return cfg.hc * cfg.hs; }
  void Free();
};

// Load the MTP draft model from `cfg.mtp_dir` (its own index + shards). The
// `main_embed` / `main_lm_head` device pointers are borrowed from the main
// model (see MtpModel). On success the caller owns the device memory (free
// with MtpModel::Free).
Status LoadMtp(const MtpConfig& cfg, const uint16_t* main_embed,
               const uint16_t* main_lm_head, MtpModel* out, cudaStream_t stream);

// Zero the MTP full-attention KV cache + indexer buffers (call before a fresh
// speculative-decoding sequence, mirroring the main model's per-layer reset).
Status MtpResetState(const MtpModel& m, cudaStream_t stream);

// Run one MTP draft step.
//
//   input_ids   : device int32 [T] — the new token(s) to embed (step 0: the
//                 last main-model token; later steps: the prior draft token).
//   positions   : host int [T] — absolute positions for the full attention.
//   hidden_states: device BF16 [T, hc*hs] — the pre-final-mixer multi stream
//                 (step 0: from the main model's trunk_out; later steps: the
//                 prior draft step's multi_hidden).
//   sample_hidden: device BF16 [T, hs] (out) — single stream for the lm_head.
//   multi_hidden : device BF16 [T, hc*hs] (out) — pre-final-mixer multi stream
//                 for the next draft step.
//   logits       : device BF16 [T, vocab] (out) — lm_head(sample_hidden).
//   stream       : CUDA stream.
Status MtpForward(const MtpModel& m, const int32_t* input_ids, const int* positions,
                  const uint16_t* hidden_states, uint16_t* sample_hidden,
                  uint16_t* multi_hidden, uint16_t* logits, int T,
                  cudaStream_t stream);

// Device bytes for the MtpForward `workspace` (the GEMM scratch plus the
// forward intermediates for `T` tokens, the full-attention scratch, and the
// BF16 MoE workspace). `full` is the MTP layer's full-attention weights (used
// to size the attention scratch).
size_t MtpWorkspaceBytes(const MtpConfig& cfg, int T,
                         const model::FullAttentionWeights& full);

// 推测解码一步: 从当前 seq 状态 (position P, 主模型 pre-final-mixer trunk
// trunk_in [hc*hs]) 出发, 跑 k 步 MTP draft, 再用主模型逐 token 验证, 接受
// 最长正确前缀 + 1 个 bonus token。
//
// 返回: accepted_count = 接受的 token 数 (1..k+1), accepted_tokens 填充
// (host, 调用方分配 >= k+1), next_trunk 填充下一轮的 trunk [hc*hs] (最后一个
// 接受 token 的主模型 pre-final-mixer 多流)。seq 的 position/history 已推进
// 到接受后状态。
//
// 约定 (scheme A, 见本文件顶部):
//   - trunk_in = 主模型在 position P-1 的 pre-final-mixer 多流 (上一轮
//     next_trunk, 首轮 = 主模型 prefill 的 trunk_out 最后一行)。
//   - draft 步 i 的 hidden_states = trunk_{P+i-1}, 产出 draft token 在
//     position P+i 与 trunk_{P+i}。
//   - 验证: 主模型在 position P+i 的 argmax 必须等于 draft token_{P+i+1}
//     (i<k); 全接受时 bonus = 主模型在 position P+k 的 argmax。
Status MtpSpeculativeStep(const model::Model& main, const MtpModel& mtp,
                          model::ModelSequence* seq, const uint16_t* trunk_in,
                          int k, int32_t* accepted_tokens, int* accepted_count,
                          uint16_t* next_trunk, cudaStream_t stream);

}  // namespace mtp
}  // namespace q4t
