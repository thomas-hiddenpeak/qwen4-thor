// Complete qwen4_exp model — orchestrates the head, the 48 decoder layers,
// and the PLE SSD-stream embedding into one forward pass.
//
// Forward (prefill of a fresh sequence, positions 0..T-1):
//   1. emb   = head.EmbedLookup(input_ids)          [T, hs]
//   2. trunk = head.ExpandTrunk(emb)                [T, hc*hs] (hc identical)
//   3. for l in [0, num_layers):
//        [if layers[l].has_ple] ple_emb = PleEmbedding::Gather(ids, history)
//                                 * weight_scale                       [T, pe]
//        trunk = DecoderLayerForward(layers[l], trunk, ple_emb, ...)
//   4. logits = head.HeadForward(trunk)             [T, vocab]
//
// The model owns all device weights (head + per-layer attn/MoE/HC/PLE) and the
// PLE embedding (io_uring SSD reader). Weights total ~84 GB (69 GB NVFP4 MoE +
// 15 GB BF16) and fit in the Thor's 128 GB unified memory. The 51 GB PLE table
// stays on NVMe and is streamed per-token by the PLE embedding.
#pragma once

#include <cuda_runtime.h>

#include <array>
#include <cstdint>
#include <string>
#include <vector>

#include "q4t/io/weight_loader.h"
#include "q4t/model/decoder_layer.h"
#include "q4t/model/model_head.h"
#include "q4t/ple/ngram_hash.h"
#include "q4t/ple/ple_embedding.h"
#include "q4t/status.h"

namespace q4t {
namespace model {

// Static configuration of the model (all from config.json / the checkpoint).
struct ModelConfig {
  std::string model_dir;
  std::string index_path;
  int num_layers = 48;  // load layers [0, num_layers)
  int hs = 2560;
  int hc = 4;
  int lowrank = 320;
  int E = 512;
  int moe_is = 640;
  int shared_is = 640;
  int topk = 10;
  int vocab = 248320;
  int image_token_id = 248056;  // <image> placeholder (vision feature injection)
  int video_token_id = 248057;  // <video> placeholder (vision feature injection)
  int spatial_merge_size = 2;  // vision merger block size (3D MRoPE grid)
  // full-attention cache length. Sized to the kernel cap (kMaxT=8192 in
  // full_attention.cu) and ple_capacity_tokens, so the QSA sparse path
  // (active once context > indexer_budget) is reachable during decode.
  int max_len = 8192;
  int max_prefill = 2048;  // sizes the forward workspace
  float eps = 1e-6f;

  // PLE SSD stream (the core differentiator).
  std::string ple_sidecar;  // path to the 51 GB FP8 n-gram table
  int64_t ple_total_rows = 320001536;
  size_t ple_row_bytes = 160;
  int ple_capacity_tokens = 8192;
  int64_t eos_token_id = 248044;
  int ple_ngram_size = 3;
  int ple_heads_per_ngram = 8;
};

// The full model: head + decoder layers + PLE embedding + persistent buffers.
struct Model {
  ModelConfig cfg;
  ModelHeadWeights head;
  std::vector<DecoderLayer> layers;  // size = cfg.num_layers
  ple::PleEmbedding* ple_emb = nullptr;  // null if no PLE layer in range
  ple::NgramHashParams ple_hash;
  float ple_weight_scale = 1.0f;

  // Persistent device buffers (allocated in LoadModel, freed in Free).
  int32_t* d_ids = nullptr;  // [max_prefill]
  int* d_positions = nullptr;  // [max_prefill] logical positions (KV paging,
                               // indexer grouping, causal mask)
  int* d_rope_pos = nullptr;  // [3, max_len] 3D MRoPE (t,h,w) for RoPE angles
  // mrope_position_delta: during incremental decode the 3 rope rows for a
  // token at logical position p are all (p + rope_delta). Computed at prefill
  // (mutable so it can be written through a const Model& in RunPrefill).
  mutable int rope_delta = 0;
  uint16_t* d_emb = nullptr;  // [max_prefill, hs]
  uint16_t* d_trunk = nullptr;  // [max_prefill, hc*hs] (ping)
  uint16_t* d_trunk2 = nullptr;  // [max_prefill, hc*hs] (pong)
  uint16_t* d_ple_emb = nullptr;  // [max_prefill, ple_embed_dim]
  int* d_img_pos = nullptr;  // [max_prefill] image-token positions (device)
  void* d_ws = nullptr;  // forward workspace (reused across layers)
  size_t ws_bytes = 0;

  // MTP speculative-verify checkpoints (lazy-allocated by
  // ModelReserveVerifyCheckpoints): per-linear-layer per-token SSM/conv state,
  // so a partial accept restores the accepted-prefix boundary via D2D instead
  // of re-running the forward. ssm = [num_lin, cap, ssm_elems] f32,
  // conv = [num_lin, cap, conv_elems] bf16.
  float* d_verify_ssm_ckpt = nullptr;
  uint16_t* d_verify_conv_ckpt = nullptr;
  int verify_ckpt_cap = 0;

  int hc_dim() const { return cfg.hc * cfg.hs; }
  void Free();
};

// Load the PLE n-gram hash params + weight_scale from the checkpoint tensors
//   {ple_prefix}.ple_embedding.layer_multipliers           I64 [ngram_size]
//   {ple_prefix}.ple_embedding.ngram_heads_vocab_sizes     I64 [ngram_heads]
//   {ple_prefix}.ple_embedding.ngram_heads_offsets         I64 [ngram_heads]
//   {ple_prefix}.ple_embedding.ngram_embedding.weight_scale BF16 [1]
Status LoadPleHashParams(const io::WeightLoader& loader,
                         const std::string& ple_prefix,
                         ple::NgramHashParams* out, float* weight_scale,
                         cudaStream_t stream);

// Load the full model (head + cfg.num_layers decoder layers + PLE embedding).
// On success the caller owns all device memory (free with Model::Free).
Status LoadModel(const ModelConfig& cfg, Model* out, cudaStream_t stream);

// Vision features for multimodal prefill (see ModelForward / ModelPrefill).
//
// The vision tower (q4t_vision) emits [num_image_tokens, hs] BF16 features,
// one row per <image> placeholder token, in spatial order. Injection mirrors
// vllm's `_merge_multimodal_embeddings`: every input_ids position equal to
// cfg.image_token_id is overwritten in the embedding buffer with the next
// feature row (the i-th image token takes feature row i). `num_tokens` must
// equal the number of image_token_id occurrences in input_ids.
struct VisionFeatures {
  const uint16_t* device = nullptr;  // [num_tokens, hs] device BF16
  int num_tokens = 0;
  // Per vision item (in prompt position order) the ViT grid (t, h, w) in
  // PATCH units (grid_t, grid_h, grid_w). Used to compute the 3D MRoPE
  // positions for the expanded image/video tokens. Empty = text-only (or the
  // caller does not need 3D positions). One entry per image_token_id /
  // video_token_id placeholder, in the same left-to-right order as the
  // feature rows.
  std::vector<std::array<int, 3>> grids;
};

// Expand image placeholders for multimodal prefill.
//
// The chat template renders each image as a single <image> token
// (image_token_id). The vision tower, however, emits `grid_h/merge *
// grid_w/merge` merged feature rows per image, so each placeholder must be
// expanded to that many image_token_id entries (the injection then overwrites
// them in order). `counts[i]` is the expansion factor for the i-th image.
// Returns false if the number of image_token_id occurrences in `ids` does not
// match `counts.size()` (a placeholder without an image, or vice versa).
inline bool ExpandImageTokens(const int32_t* ids, int T, int image_token_id,
                              const std::vector<int>& counts,
                              std::vector<int32_t>* out) {
  out->clear();
  out->reserve(static_cast<size_t>(T) +
               static_cast<size_t>(counts.size()));
  size_t img_idx = 0;
  for (int i = 0; i < T; ++i) {
    if (ids[i] == image_token_id) {
      if (img_idx >= counts.size()) return false;
      const int c = counts[img_idx];
      if (c <= 0) return false;
      for (int k = 0; k < c; ++k) out->push_back(image_token_id);
      ++img_idx;
    } else {
      out->push_back(ids[i]);
    }
  }
  return img_idx == counts.size();
}

// Expand BOTH image and video placeholders in a single pass. Each
// image_token_id occurrence expands by the next image_count, each
// video_token_id occurrence by the next video_count (independent counters,
// left-to-right). Returns false if a placeholder has no matching count, or if
// any count is left unconsumed.
inline bool ExpandMultimodalTokens(
    const int32_t* ids, int T, int image_token_id, int video_token_id,
    const std::vector<int>& image_counts, const std::vector<int>& video_counts,
    std::vector<int32_t>* out) {
  out->clear();
  out->reserve(static_cast<size_t>(T) + image_counts.size() +
               video_counts.size());
  size_t img_idx = 0, vid_idx = 0;
  for (int i = 0; i < T; ++i) {
    if (ids[i] == image_token_id) {
      if (img_idx >= image_counts.size()) return false;
      const int c = image_counts[img_idx];
      if (c <= 0) return false;
      for (int k = 0; k < c; ++k) out->push_back(image_token_id);
      ++img_idx;
    } else if (ids[i] == video_token_id) {
      if (vid_idx >= video_counts.size()) return false;
      const int c = video_counts[vid_idx];
      if (c <= 0) return false;
      for (int k = 0; k < c; ++k) out->push_back(video_token_id);
      ++vid_idx;
    } else {
      out->push_back(ids[i]);
    }
  }
  return img_idx == image_counts.size() && vid_idx == video_counts.size();
}

// Run one prefill forward: input_ids [T] (host int32) -> logits [T, vocab]
// (device BF16). T must be <= cfg.max_prefill. Prefill starts from empty
// per-layer state (positions 0..T-1).
//
// `vision` (optional): if non-null and vision->num_tokens > 0, the image
// token embeddings are replaced with the vision features (see
// VisionFeatures). Null = pure-text prefill.
Status ModelForward(const Model& m, const int32_t* input_ids, int T,
                    uint16_t* logits, cudaStream_t stream,
                    const VisionFeatures* vision = nullptr);

// Run one decode step for a single new token: token_id at absolute position
// `position` -> logits [1, vocab] (device BF16). Per-layer state is NOT reset
// (it continues from the prior steps, so call ModelForward first for the
// prompt). `history` is the host int32 array of the tokens already generated
// before this one (prompt + previously decoded tokens), length >= position;
// it supplies the PLE n-gram context for the new token.
//
// `trunk_out` (MTP scheme A hook, optional): if non-null, the pre-final-mixer
// multi stream [T, hc*hs] (device BF16, row-major) is copied here — the trunk
// after the last decoder layer, before hyper_connection_mixer. This is the
// input the MTP draft model consumes as `hidden_states` (see
// reference/vllm/vllm/models/qwen4_exp/nvidia/mtp.py). Null = not exposed.
Status ModelDecodeStep(const Model& m, int32_t token_id, int position,
                       const int32_t* history, uint16_t* logits,
                       cudaStream_t stream, uint16_t* trunk_out = nullptr);

// Batched decode over T tokens at absolute positions [base_position ..
// base_position+T-1] WITHOUT resetting per-layer state (continues from the
// current KV/SSM/conv). Returns [T, vocab] logits (and optionally [T, hc*hs]
// trunk_out). This lets MTP verify k+1 speculative tokens in ONE bandwidth-
// bound forward (weights read once) instead of k+1 separate T=1 decodes.
// `history` (length `history_len`) supplies the PLE n-gram context for the
// positions before `base_position`; the in-batch prefix supplies the rest.
// The caller owns `logits` (>= T*vocab) and `trunk_out` (>= T*hc*hs).
Status ModelDecodeBatch(const Model& m, const int32_t* input_ids, int T,
                        int base_position, const int32_t* history,
                        int history_len, uint16_t* logits, cudaStream_t stream,
                        uint16_t* trunk_out = nullptr,
                        bool save_checkpoints = false);

// Reserve per-linear-layer per-token SSM/conv checkpoint buffers for MTP
// verify (idempotent; grows if num_ckpt exceeds the current capacity). Must be
// called before ModelDecodeBatch(save_checkpoints=true) / ModelRestoreCheckpoint.
Status ModelReserveVerifyCheckpoints(Model& m, int num_ckpt);

// Restore every linear layer's SSM/conv state from checkpoint `ckpt_idx` (D2D).
// Used after a partial-accept MTP verify to roll the recurrent state back to
// the accepted-prefix boundary WITHOUT re-running the forward.
Status ModelRestoreCheckpoint(const Model& m, int ckpt_idx, cudaStream_t stream);

// ---------------------------------------------------------------------------
// PD-ready 阶段边界 API (Prefill/Decode 可分离, 见 ARCHITECTURE.md)。
//
// ModelSequence 是 runner 侧的轻量序列状态机 (host only, 不拥有 device
// 内存): 跟踪阶段 (prefill -> decode)、绝对 position、PLE n-gram history。
// 它让 runner 能把 prefill 与 decode 驱动为独立操作:
//
//   ModelBeginSequence(seq)          // 重置 per-layer 状态 (KV/SSM/conv),
//                                    // 阶段 = kPrefill
//   ModelPrefill(m, seq, ids, T, logits)   // 完成 prefill, 阶段 = kDecode;
//                                    // 此时 per-layer KV/SSM 状态已就绪,
//                                    // 可整体交出 (PD 分离的 handoff 点)
//   ModelDecodeStepSeq(m, seq, tok, logits) // 一个 decode token, 自动维护
//                                    // position 与 PLE history
//   ModelEndSequence(seq)            // 序列结束 (状态机复位, 不释放 device
//                                    // 内存 — 内存归 Model 所有)
//
// 与 ModelForward/ModelDecodeStep 等价 (ModelForward = Begin + Prefill),
// 但阶段边界显式化, 供 runner 的 PD 分离场景驱动。
struct ModelSequence {
  enum class Stage { kIdle, kPrefill, kDecode };
  Stage stage = Stage::kIdle;
  int position = 0;  // 下一个 token 的绝对 position
  std::vector<int32_t> history;  // 已见 token (prompt + 已 decode), PLE 上下文
};

// 重置 per-layer 状态 (KV/SSM/conv 清零), 序列进入 kPrefill 阶段。
// 等价于 ModelForward 内部的 ResetState 循环。
Status ModelBeginSequence(const Model& m, ModelSequence* seq,
                          cudaStream_t stream);

// 完成 prefill: seq 须处于 kPrefill 阶段。input_ids [T] -> logits [T, vocab]
// (device BF16)。成功后阶段 = kDecode, position = T, history = prompt。
// 调用返回时 per-layer KV/SSM 状态已就绪 (PD 分离的 handoff 点)。
// trunk_out: 可选, 非 null 时把 pre-final-mixer 多流 [T, hc*hs] 拷入
// (MTP scheme A 钩子, 见 ModelDecodeStep 的说明)。
// vision: 可选, 非 null 且 num_tokens>0 时把 image token 的 embedding 替换为
// 视觉特征 (见 VisionFeatures)。
Status ModelPrefill(const Model& m, ModelSequence* seq, const int32_t* input_ids,
                    int T, uint16_t* logits, cudaStream_t stream,
                    uint16_t* trunk_out = nullptr,
                    const VisionFeatures* vision = nullptr);

// 一个 decode step: seq 须处于 kDecode 阶段。token_id 写入 position,
// -> logits [1, vocab]。自动 ++position 并追加 history (PLE 上下文)。
// 等价于 ModelDecodeStep (history 由 seq 内部维护)。trunk_out 同上。
Status ModelDecodeStepSeq(const Model& m, ModelSequence* seq, int32_t token_id,
                          uint16_t* logits, cudaStream_t stream,
                          uint16_t* trunk_out = nullptr);

// 序列结束: 状态机复位为 kIdle (不释放 device 内存, 内存归 Model 所有)。
void ModelEndSequence(ModelSequence* seq);

// ---------------------------------------------------------------------------
// 主模型 recurrent 状态快照 (推测解码回滚用)。
//
// 主模型的 per-layer 状态分两类:
//   - paged (full-attention KV + indexer): 按绝对 position 写入, 单调增长。
//     推测解码重跑同一 position 会覆盖同一 page, 无需回滚。
//   - recurrent (linear SSM/conv, PLE conv): 原地递推, 无法按 position 回退。
//     推测解码验证 k 个 draft token 后若只接受 a<k 个, 必须回滚到验证前的
//     recurrent 状态再重跑 a 个 token。
//
// ModelStateSnapshot 只持有 recurrent 状态 (paged 状态不动)。
struct ModelStateSnapshot {
  std::vector<uint8_t> data;  // 所有 recurrent 状态拼接
  size_t bytes = 0;
  bool valid = false;
  void Free();
};

// 所有 recurrent 状态的 device 字节数 (SSM FP32 + conv BF16 + PLE conv BF16)。
size_t ModelStateSnapshotBytes(const Model& m);

// 快照当前 recurrent 状态 (device -> host)。验证前调用。
Status ModelSnapshotState(const Model& m, ModelStateSnapshot* snap,
                          cudaStream_t stream);

// 恢复 recurrent 状态 (host -> device)。验证后回滚调用。
Status ModelRestoreState(const Model& m, const ModelStateSnapshot& snap,
                         cudaStream_t stream);

}  // namespace model
}  // namespace q4t
