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
  // Number of concurrent sequences the draft full-attention KV/indexer state
  // is pooled for (Phase 2 MTP continuous batching). Each sequence owns an
  // independent [seq_id, ...] slice, enabling concurrent MTP requests. 1 =
  // legacy single-sequence behavior (bit-identical, no extra memory).
  int max_seq = 1;
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
  int* d_rope_pos = nullptr;  // [3, max_len] identity (pure text, shared by
  // all sequences — the identity table is the same for every seq)
  void* d_ws = nullptr;  // forward workspace (GEMM ws + scratch)
  size_t ws_bytes = 0;
  size_t kv_bytes = 0;  // bytes of ONE sequence's kv_cache (for ResetState)
  size_t idx_bytes = 0;  // bytes of ONE sequence's idx_raw (== idx_comp)
  int max_seq = 1;  // pooled sequence count (cfg.max_seq at load time)

  // Speculative-step scratch (allocated in LoadMtp, sized for one draft step
  // of a single token): the sample_hidden [hs], the multi_hidden trunk [hc*hs],
  // and the logits [vocab].
  uint16_t* d_sample = nullptr;
  uint16_t* d_trunk = nullptr;
  uint16_t* d_logits = nullptr;

  // Per-step speculative scratch (allocated in LoadMtp, sized for k up to
  // k_max). Preallocating these avoids the per-step cudaMalloc/cudaFree in
  // MtpSpeculativeStep / MtpDraftExtend — each cudaFree is an implicit device
  // sync that stalls the pipeline. When the requested k exceeds k_max the
  // caller falls back to per-call allocation.
  int32_t* d_ids_scratch = nullptr;   // [k_max]
  int* d_pos_scratch = nullptr;       // [k_max]
  uint16_t* d_spec_logits = nullptr;  // [k_max*vocab] verify logits + extend logits
  uint16_t* d_spec_trunk = nullptr;   // [k_max*hc_dim] verify trunk (extend input)
  uint16_t* d_spec_multi = nullptr;   // [k_max*hc_dim] extend multi_hidden (output)
  uint16_t* d_spec_sample = nullptr;  // [k_max*hs] extend sample_hidden
  uint16_t* d_g = nullptr;            // [hc_dim] rolling draft trunk
  // Multi-seq speculative scratch (Phase 2 Stage 2b, allocated in
  // MtpReserveScratch when max_seq > 1). The batched draft loop packs B
  // sequences' tokens per step (T = B), so it needs its own ids/positions/
  // sample/multi buffers sized for B rows (B <= max_seq); the batched extend
  // packs the accepted prefixes of all sequences (T = sum(a_b+1) <= B*(k+1)),
  // sized for the worst case. d_ms_g_pool = per-sequence rolling draft trunk
  // [max_seq, hc_dim] (the batched draft loop reads row b as sequence b's
  // hidden and overwrites it with the step's multi_hidden); d_ms_gather =
  // extend hidden gather [B*(k+1), hc_dim] (per-sequence verify-trunk rows
  // copied contiguous for the batched extend MtpForward, which needs a
  // contiguous [T, hc_dim] hidden_states).
  int32_t* d_ms_ids = nullptr;     // [max_seq] draft-loop packed ids
  int* d_ms_pos = nullptr;         // [max_seq] draft-loop positions
  int* d_ms_seqid = nullptr;       // [max_seq] identity (draft-loop d_seq_id:
                                   // token t belongs to sequence t)
  uint16_t* d_ms_sample = nullptr;  // [max_seq*hs] draft-loop sample_hidden
  uint16_t* d_ms_multi = nullptr;   // [max_seq*hc_dim] draft-loop multi_hidden
  uint16_t* d_ms_gather = nullptr;  // [B*(k+1)*hc_dim] extend hidden gather
  uint16_t* d_ms_g_pool = nullptr;  // [max_seq*hc_dim] per-seq rolling trunk
  int* d_ms_ext_seq = nullptr;      // [max_seq*k_max] extend per-token d_seq_id
  int k_max = 0;  // capacity of the above (0 = not allocated)

  int hc_dim() const { return cfg.hc * cfg.hs; }
  void Free();
};

// Load the MTP draft model from `cfg.mtp_dir` (its own index + shards). The
// `main_embed` / `main_lm_head` device pointers are borrowed from the main
// model (see MtpModel). On success the caller owns the device memory (free
// with MtpModel::Free).
Status LoadMtp(const MtpConfig& cfg, const uint16_t* main_embed,
               const uint16_t* main_lm_head, MtpModel* out, cudaStream_t stream);

// Bytes of ONE sequence's draft full-attention KV cache (the pooled layout is
// [max_seq, n_pages, kKvPageSize, nkv, 2, hd] uint16; this is the per-seq
// stride). Useful for sizing pooled buffers and per-seq resets.
size_t MtpPerSeqKvBytes(const MtpConfig& cfg);

// Zero the MTP full-attention KV cache + indexer buffers (call before a fresh
// speculative-decoding sequence, mirroring the main model's per-layer reset).
// `seq_id` selects one pooled sequence slice (valid only when
// m.max_seq > 1); pass -1 to reset ALL sequences (the single-sequence layout
// when m.max_seq == 1, or the whole pool otherwise).
Status MtpResetState(const MtpModel& m, cudaStream_t stream, int seq_id = -1);

// Reserve the per-step speculative scratch buffers (ids/positions, verify
// logits+trunk, extend logits+trunk+sample, rolling draft trunk) sized for a
// speculative `k` up to `k_max`. Idempotent: grows only if `k_max` exceeds the
// current capacity. Call once after LoadMtp, before the decode loop, so
// MtpSpeculativeStep / MtpDraftExtend can use the persistent buffers instead of
// per-step cudaMalloc/cudaFree (each cudaFree is an implicit device sync).
Status MtpReserveScratch(MtpModel& m, int k_max);

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
//   d_seq_id     : device int[T] (Phase 2 MTP multi-seq draft). When non-null,
//                  the draft full-attention KV/indexer buffers (m.kv_cache /
//                  m.idx_raw / m.idx_comp) are the POOLED bases ([max_seq, ...])
//                  and each token t indexes the slice for d_seq_id[t] (the
//                  attention kernels select the per-token base via d_seq_id).
//                  When null (legacy single-sequence) the buffers are the
//                  per-sequence slices and the kernels use them directly
//                  (bit-identical to the prior behavior). The projection/MoE/
//                  lm_head GEMMs are stateless and operate on the packed [T,...]
//                  rows (weights read once).
Status MtpForward(const MtpModel& m, const int32_t* input_ids, const int* positions,
                  const uint16_t* hidden_states, uint16_t* sample_hidden,
                  uint16_t* multi_hidden, uint16_t* logits, int T,
                  cudaStream_t stream, const int* d_seq_id = nullptr);

// Device bytes for the MtpForward `workspace` (the GEMM scratch plus the
// forward intermediates for `T` tokens, the full-attention scratch, and the
// BF16 MoE workspace). `full` is the MTP layer's full-attention weights (used
// to size the attention scratch).
size_t MtpWorkspaceBytes(const MtpConfig& cfg, int T,
                         const model::FullAttentionWeights& full);

// Draft-extend: run the MTP draft model over T tokens to build its
// full-attention KV/indexer cache for those absolute positions, mirroring the
// vLLM MTP proposer's "first pass". This MUST be called (over the prompt)
// after the main prefill and MtpResetState, before the first MtpSpeculativeStep
// — otherwise the draft attention sees an empty KV and outputs garbage.
//
// EAGLE-style shift (see reference/.../llm_base_proposer.py set_inputs_first_pass):
//   at absolute position p, input token = the token at position p+1, hidden =
//   the main model's pre-final-mixer trunk at position p. The output at the
//   LAST row predicts the first speculative token and yields the draft trunk to
//   seed the speculative step's draft loop.
//
//   shifted_ids : host int32 [T] — for a prompt t_0..t_{P-1} extended after the
//                 main model sampled t_P, this is [t_1, t_2, ..., t_{P-1}, t_P].
//   main_trunk  : device BF16 [T, hc*hs] — the main model's per-position trunk
//                 (prefill trunk_out, or the speculative step's captured trunks).
//   positions   : host int [T] — the absolute positions p (0..T-1 for a prompt).
//   out_d0      : host — argmax of the last row's logits (first draft token).
//   out_g       : device BF16 [hc*hs] — the last row's multi_hidden (draft trunk
//                 that seeds the next draft step).
Status MtpDraftExtend(const MtpModel& m, const int32_t* shifted_ids,
                      const uint16_t* main_trunk, const int* positions, int T,
                      int32_t* out_d0, uint16_t* out_g, cudaStream_t stream);

// 推测解码一步 (scheme A, 见本文件顶部 + reference/.../nvidia/mtp.py)。
//
// 前置条件: MTP 的 full-attention KV 已通过 MtpDraftExtend 建到 position P-1
// (首步在 prefill 后对整个 prompt 做 extend; 后续步由上一步内部 extend 续上)。
// seq 处于 kDecode 阶段, seq->position = P。
//
// 输入:
//   b   = 主模型在 position P-1 的 argmax (bonus token t_P; 首步来自 prefill
//         末行, 后续步来自上一步的 next_b)。
//   d0  = draft 对 t_{P+1} 的预测 (来自上一次 extend 的末行 logits)。
//   g_in= draft 在 position P-1 的 multi_hidden [hc*hs] (来自上一次 extend),
//         作为 draft 循环第 1 步的 hidden。
//   k   = 推测步数 (draft 生成 d_0..d_{k-1})。
//
// 流程:
//   1. draft 循环: d_0=d0 已给, 逐 token 生成 d_1..d_{k-1} (input=d_{j-1},
//      position=P+j-1, hidden=上一步 multi_hidden), 写 draft KV[P..P+k-2]。
//   2. 惰性验证 (无需回滚): 喂 b@P → m_0; 逐个比对 d_i==m_i, 命中则喂 d_i@P+1+i
//      → m_{i+1}, 只喂被接受的 token, 主模型状态恰好推进到接受前缀末尾。
//   3. 输出接受 token = [b, d_0..d_{a-1}] (count=1+a); next_b = m_a (修正/下一
//      bonus)。
//   4. 内部 extend: 用验证期捕获的主干 h_P..h_{P+a} 对 [d_0..d_{a-1}, next_b]
//      在 position P..P+a 重建 draft KV (覆盖投机写入), 产出 next_d0 + next_g。
//
// 返回: accepted_tokens 填 1+a 个 (host, 调用方 >= k+1); next_b/next_d0/next_g
// 供下一步。seq 已推进 1+a。
Status MtpSpeculativeStep(const model::Model& main, const MtpModel& mtp,
                          model::ModelSequence* seq, int32_t b, int32_t d0,
                          const uint16_t* g_in, int k, int32_t* accepted_tokens,
                          int* accepted_count, int32_t* next_b,
                          int32_t* next_d0, uint16_t* next_g,
                          cudaStream_t stream);

// 多序列投机步 (Phase 2 Stage 2b): B 个序列各走一步投机解码, 打包执行。
// 与 MtpSpeculativeStep 同语义 (scheme A, EAGLE-shift), 但三个段都批量化:
//
//   1. 批量化 draft 循环: 每步 j 把 B 序列的 draft[j-1] 打包成一次
//      MtpForward (d_seq_id, T=B, 每序列 1 token — Stage 1 的多序列路径),
//      滚动 trunk 用 per-seq 池 d_g_pool [max_seq, hc_dim] (行 b = 序列 b)。
//      draft 循环从 B×k 次 forward 降到 k 次。
//   2. 多序列验证: 每序列 (k+1) token 打包一次主模型 forward
//      (ModelVerifyMulti, Stage 2a), 权重只读一次; per-seq per-token
//      checkpoint 保存, 部分接受按序列各自回滚 (ModelRestoreCheckpoint)。
//   3. per-seq extend: 各序列用接受前缀 [d_0..d_{a_b-1}, next_b] 重建
//      draft KV (一次打包 MtpForward, T = Σ(a_b+1), 各序列行连续,
//      EAGLE-shift 与单序列一致), 产出每序列的 next_d0 + next_g。
//
// 前置条件: 每序列的 MTP draft KV 已通过 MtpDraftExtend 建到 P_b-1
// (per-seq 切片, MtpResetState(m, stream, b) 重置); 主模型 per-seq recurrent
// 状态在 P_b-1 (已 prefill)。
//
//   seqs[b]  : 主模型序列状态机 (kDecode, position = P_b, history = 已见
//              token)。本函数推进 position/history (接受前缀), 与单序列一致。
//   b_tok[b] : 主模型 bonus token t_{P_b} (host int32)。
//   d0[b]    : draft 对 t_{P_b+1} 的预测 (host int32, 来自上次 extend)。
//   g_in[b]  : draft 在 P_b-1 的 multi_hidden (device BF16 [hc*hs], 来自上次
//              extend 的 next_g; 拷入 d_g_pool 行 b 作 draft 循环种子)。
//   k        : 推测步数 (各序列相同; ragged k 留待后续)。
//   accepted_tokens[b*(k+1) .. ] : 每序列 1+a_b 个接受 token (host, 调用方
//              每序列预留 k+1)。
//   accepted_count[b] / next_b[b] / next_d0[b] : 每序列输出 (host)。
//   next_g[b] : 每序列下一步 draft trunk (device BF16 [hc*hs], 调用方持有,
//              各序列独立 buffer)。
//
// 正确性: draft 循环与 extend 复用 Stage 1 已验证的多序列 MtpForward
// (per-seq KV 隔离); 验证复用 Stage 2a 已 bit-exact 验证的 ModelVerifyMulti。
// 单序列 (B=1) 不走本函数 (用 MtpSpeculativeStep, 路径更省)。
Status MtpSpeculativeStepMulti(const model::Model& main, const MtpModel& mtp,
                               model::ModelSequence* seqs, const int32_t* b_tok,
                               const int32_t* d0, const uint16_t* const* g_in,
                               int B, int k, int32_t* accepted_tokens,
                               int* accepted_count, int32_t* next_b,
                               int32_t* next_d0, uint16_t** next_g,
                               cudaStream_t stream);

}  // namespace mtp
}  // namespace q4t
