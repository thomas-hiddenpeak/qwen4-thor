// Complete qwen4_exp model — implementation. See include/q4t/model/model.h.
//
// ModelForward (prefill, fresh sequence, positions 0..T-1):
//   1. d_ids = input_ids; positions = 0..T-1
//   2. emb = head.EmbedLookup(d_ids)            [T, hs]
//   3. trunk = head.ExpandTrunk(emb)            [T, hc*hs]
//   4. for l in [0, num_layers):
//        [if layers[l].has_ple]
//          history[t] = ngram_size-1 preceding tokens (EOS-filled at start)
//          ple_emb = PleEmbedding::Gather(ids, history)   [T, pe] (SSD stream)
//          ple_emb *= weight_scale
//        trunk = DecoderLayerForward(layers[l], trunk, ple_emb, positions, ...)
//   5. logits = head.HeadForward(trunk)         [T, vocab]
//
// The trunk ping-pongs between d_trunk / d_trunk2 so each layer reads one
// buffer and writes the other. The forward workspace d_ws is reused across
// layers (they run sequentially); its size is the max over all layers.
#include "q4t/model/model.h"

#include <cuda_bf16.h>
#include <cuda_runtime.h>

#include <algorithm>
#include <cstdint>
#include <cstring>
#include <memory>
#include <string>
#include <utility>
#include <vector>

#include "q4t/model/linear.h"

namespace q4t {
namespace model {

namespace {

constexpr int kBlock = 256;

// ple_emb[i] *= scale (in place, BF16).
__global__ void ScaleBf16Kernel(uint16_t* __restrict__ x, int total,
                                float scale) {
  const int i = blockIdx.x * blockDim.x + threadIdx.x;
  if (i >= total) return;
  uint32_t bits = static_cast<uint32_t>(x[i]) << 16;
  float f;
  std::memcpy(&f, &bits, sizeof(f));
  const __nv_bfloat16 r = __float2bfloat16_rn(f * scale);
  x[i] = *reinterpret_cast<const uint16_t*>(&r);
}

// Replace image-token embeddings with vision features (multimodal injection).
// d_img_pos[i] = the position of the i-th <image> token in the sequence;
// vision_src[i*hs .. (i+1)*hs] (device BF16) is copied over d_emb at that
// position. One thread per (i, j) element. Mirrors vllm's
// `_merge_multimodal_embeddings` (inputs_embeds[is_multimodal] = mm_embeds).
__global__ void InjectVisionKernel(const uint16_t* __restrict__ vision_src,
                                   const int* __restrict__ d_img_pos,
                                   uint16_t* __restrict__ d_emb, int hs,
                                   int num_tokens) {
  const int i = blockIdx.x * blockDim.x + threadIdx.x;
  const int total = num_tokens * hs;
  if (i >= total) return;
  const int tok = i / hs;
  const int j = i - tok * hs;
  const int pos = d_img_pos[tok];
  d_emb[pos * hs + j] = vision_src[i];
}

}  // namespace

void Model::Free() {
  head.Free();
  for (auto& l : layers) l.Free();
  layers.clear();
  if (ple_emb) delete ple_emb;
  ple_emb = nullptr;
  auto freep = [](void* p) {
    if (p) cudaFree(p);
  };
  freep(d_ids);
  freep(d_positions);
  freep(d_seq_id);
  freep(d_token_seq_id);
  freep(d_ragged_seq_offset);
  freep(d_ragged_token_local);
  freep(d_rope_pos);
  freep(d_emb);
  freep(d_trunk);
  freep(d_trunk2);
  freep(d_ple_emb);
  freep(d_img_pos);
  freep(d_ws);
  freep(d_verify_ssm_ckpt);
  freep(d_verify_conv_ckpt);
  freep(d_verify_ple_conv_ckpt);
  d_ids = nullptr;
  d_positions = nullptr;
  d_seq_id = nullptr;
  d_token_seq_id = nullptr;
  d_ragged_seq_offset = nullptr;
  d_ragged_token_local = nullptr;
  d_rope_pos = nullptr;
  d_emb = nullptr;
  d_trunk = nullptr;
  d_trunk2 = nullptr;
  d_ple_emb = nullptr;
  d_img_pos = nullptr;
  d_ws = nullptr;
  d_verify_ssm_ckpt = nullptr;
  d_verify_conv_ckpt = nullptr;
  d_verify_ple_conv_ckpt = nullptr;
  verify_ckpt_cap = 0;
  ws_bytes = 0;
}

Status LoadPleHashParams(const io::WeightLoader& loader,
                         const std::string& ple_prefix,
                         ple::NgramHashParams* out, float* weight_scale,
                         cudaStream_t stream) {
  (void)stream;
  const std::string base = ple_prefix + ".ple_embedding.";
  // layer_multipliers: I64 [ngram_size]
  {
    const io::TensorInfo* info = loader.FindTensor(base + "layer_multipliers");
    if (!info) return Status::Fail("missing layer_multipliers");
    out->multipliers.assign(info->numel(), 0);
    Status s = loader.ReadTensor(base + "layer_multipliers",
                                 out->multipliers.data());
    if (!s.ok()) return s;
  }
  // ngram_heads_vocab_sizes: I64 [ngram_heads]
  {
    const io::TensorInfo* info =
        loader.FindTensor(base + "ngram_heads_vocab_sizes");
    if (!info) return Status::Fail("missing ngram_heads_vocab_sizes");
    out->head_vocab_sizes.assign(info->numel(), 0);
    Status s = loader.ReadTensor(base + "ngram_heads_vocab_sizes",
                                 out->head_vocab_sizes.data());
    if (!s.ok()) return s;
  }
  // ngram_heads_offsets: I64 [ngram_heads]
  {
    const io::TensorInfo* info =
        loader.FindTensor(base + "ngram_heads_offsets");
    if (!info) return Status::Fail("missing ngram_heads_offsets");
    out->head_offsets.assign(info->numel(), 0);
    Status s = loader.ReadTensor(base + "ngram_heads_offsets",
                                 out->head_offsets.data());
    if (!s.ok()) return s;
  }
  // ngram_embedding.weight_scale: BF16 [1]
  {
    uint16_t raw = 0;
    Status s = loader.ReadTensor(base + "ngram_embedding.weight_scale", &raw);
    if (!s.ok()) return s;
    uint32_t bits = static_cast<uint32_t>(raw) << 16;
    std::memcpy(weight_scale, &bits, sizeof(float));
  }
  return Status();
}

Status LoadModel(const ModelConfig& cfg, Model* m, cudaStream_t stream) {
  m->cfg = cfg;
  // RAII: the mmap'd safetensors shards must be released on every exit path.
  // On Jetson Thor's 122 GB unified memory, keeping the ~84 GB of file
  // mappings alive alongside the ~84 GB of GPU weights exceeds the budget.
  // (Same fix as qwen35-thor model.cpp: "统一内存: 立即释放 mmap".)
  std::unique_ptr<io::WeightIndex> index;
  {
    io::WeightIndex* raw = nullptr;
    Status s = io::WeightIndex::Open(cfg.index_path, &raw);
    if (!s.ok()) return s;
    index.reset(raw);
  }
  std::unique_ptr<io::WeightLoader> loader;
  {
    io::WeightLoader* raw = nullptr;
    Status s = io::WeightLoader::Create(cfg.model_dir, *index, 8, &raw);
    if (!s.ok()) return s;
    loader.reset(raw);
  }
  Status s;

  const int hc_dim = cfg.hc * cfg.hs;

  // 1. Head (embed + lm_head + mixer).
  s = LoadModelHead(*loader, cfg.vocab, cfg.hs, cfg.hc, cfg.lowrank, cfg.eps,
                    &m->head, stream);
  if (!s.ok()) return s;

  // 2. Decoder layers [0, num_layers).
  m->layers.resize(cfg.num_layers);
  for (int l = 0; l < cfg.num_layers; ++l) {
    s = LoadDecoderLayer(*loader, l, cfg.hs, cfg.hc, cfg.lowrank, cfg.eps,
                         cfg.E, cfg.moe_is, cfg.shared_is, cfg.topk,
                         cfg.max_len, cfg.max_seq, &m->layers[l], stream);
    if (!s.ok()) return s;
  }

  // 3. PLE embedding (only if a PLE layer is in range).
  bool any_ple = false;
  for (auto& l : m->layers) any_ple = any_ple || l.has_ple;
  if (any_ple) {
    // Load hash params + weight_scale from the (first) PLE layer.
    int ple_layer = -1;
    for (int l = 0; l < cfg.num_layers; ++l) {
      if (m->layers[l].has_ple) {
        ple_layer = l;
        break;
      }
    }
    const std::string ple_prefix =
        "model.language_model.layers." + std::to_string(ple_layer) + ".ple";
    m->ple_hash.ngram_size = cfg.ple_ngram_size;
    m->ple_hash.heads_per_ngram = cfg.ple_heads_per_ngram;
    s = LoadPleHashParams(*loader, ple_prefix, &m->ple_hash,
                          &m->ple_weight_scale, stream);
    if (!s.ok()) return s;

    ple::PleEmbedding::Config pcfg;
    pcfg.sidecar_path = cfg.ple_sidecar;
    pcfg.row_bytes = cfg.ple_row_bytes;
    pcfg.total_rows = cfg.ple_total_rows;
    pcfg.hash_params = m->ple_hash;
    pcfg.eos_token_id = cfg.eos_token_id;
    pcfg.capacity_tokens = cfg.ple_capacity_tokens;
    s = ple::PleEmbedding::Create(pcfg, &m->ple_emb);
    if (!s.ok()) return s;
  }

  // 4. Persistent forward buffers.
  const size_t max_t = static_cast<size_t>(cfg.max_prefill);
  auto alloc = [](void** p, size_t bytes) -> Status {
    if (cudaMalloc(p, bytes) != cudaSuccess) return Status::Fail("cudaMalloc");
    return Status();
  };
  if (!(s = alloc(reinterpret_cast<void**>(&m->d_ids), max_t * sizeof(int32_t))))
    return s;
  if (!(s = alloc(reinterpret_cast<void**>(&m->d_positions),
                  max_t * sizeof(int))))
    return s;
  // B2 multi-seq decode: per-token pooled-state slice index (H2D per forward,
  // sized for the max batch of concurrent sequences).
  if (!(s = alloc(reinterpret_cast<void**>(&m->d_seq_id),
                  static_cast<size_t>(cfg.max_seq) * sizeof(int))))
    return s;
  // Phase 2 MTP multi-seq verify: per-token seq id for the B*T packed verify
  // tokens (sized for the max prefill, since B*T <= max_prefill).
  if (!(s = alloc(reinterpret_cast<void**>(&m->d_token_seq_id),
                  max_t * sizeof(int))))
    return s;
  // Ragged batched prefill: cu_seqlens [max_seq+1] + per-token local position
  // [max_prefill]. Filled per ModelPrefillBatch, null in every other forward.
  if (!(s = alloc(reinterpret_cast<void**>(&m->d_ragged_seq_offset),
                  static_cast<size_t>(cfg.max_seq + 1) * sizeof(int))))
    return s;
  if (!(s = alloc(reinterpret_cast<void**>(&m->d_ragged_token_local),
                  max_t * sizeof(int))))
    return s;
  // Persistent 3D MRoPE table: [max_seq, 3, max_len] (t, h, w) rows, POOLED
  // over concurrent sequences (Phase 2). Each sequence's rope coordinates are
  // written at prefill and read across its decode steps, so the table must be
  // per-sequence (a shared table would be clobbered by a concurrent prefill).
  // Sized for the full sequence capacity (not max_prefill) because the
  // compressed-key builder reads the rope position of a group's first token,
  // which can be anywhere in the sequence during decode.
  if (!(s = alloc(reinterpret_cast<void**>(&m->d_rope_pos),
                  static_cast<size_t>(cfg.max_seq) * 3u *
                      static_cast<size_t>(cfg.max_len) * sizeof(int))))
    return s;
  // Per-sequence mrope_position_delta (written at prefill, read at decode).
  m->rope_delta.assign(static_cast<size_t>(cfg.max_seq), 0);
  if (!(s = alloc(reinterpret_cast<void**>(&m->d_emb),
                  max_t * cfg.hs * 2)))
    return s;
  if (!(s = alloc(reinterpret_cast<void**>(&m->d_img_pos),
                  max_t * sizeof(int))))
    return s;
  if (!(s = alloc(reinterpret_cast<void**>(&m->d_trunk),
                  max_t * hc_dim * 2)))
    return s;
  if (!(s = alloc(reinterpret_cast<void**>(&m->d_trunk2),
                  max_t * hc_dim * 2)))
    return s;
  if (m->ple_emb) {
    const size_t pe = m->ple_emb->ple_embed_dim();
    if (!(s = alloc(reinterpret_cast<void**>(&m->d_ple_emb),
                    max_t * pe * 2)))
      return s;
  }

  // Forward workspace: max over layers (and the head).
  size_t ws = ModelHeadWorkspaceBytes(cfg.max_prefill, cfg.hs);
  for (auto& l : m->layers) {
    ws = std::max(ws, DecoderLayerWorkspaceBytes(cfg.max_prefill,
                                                l.is_full_attention, l.has_ple,
                                                cfg.hs, cfg.E, cfg.moe_is,
                                                cfg.shared_is, cfg.topk,
                                                &l.full));
  }
  m->ws_bytes = ws;
  if (!(s = alloc(&m->d_ws, ws))) return s;

  // All weights are on GPU. `loader`/`index` (unique_ptr) release the
  // mmap'd safetensors shards at scope exit, dropping the file mappings
  // before the decode loop runs. Sync first so every H2D copy has landed
  // (the reads are async on `stream`).
  cudaStreamSynchronize(stream);
  return Status();
}

// Shared layer loop over [0, num_layers), reading `trunk`/`next` (ping-pong)
// and the per-token PLE embeddings `ple_emb` (null when no PLE layer in range).
// `ids64` / `hist` are the host int64 token ids and ngram history used to
// gather the PLE embedding at the PLE layer.
// `trunk_out` (MTP scheme A hook, optional): if non-null, the pre-final-mixer
// multi stream [T, hc*hs] (the trunk after the last decoder layer, before
// hyper_connection_mixer) is copied here. This is the MTP draft model's
// `hidden_states` input (see reference/vllm/.../nvidia/mtp.py).
Status RunLayers(const Model& m, const uint16_t* trunk_in, uint16_t* trunk2,
                 const int64_t* ids64, const int64_t* hist, int T,
                 uint16_t* logits, cudaStream_t stream,
                 uint16_t* trunk_out = nullptr, float* ssm_ckpt = nullptr,
                 uint16_t* conv_ckpt = nullptr, int num_ckpt = 0,
                 int seq_id = 0, const int* d_seq_id = nullptr,
                 int tokens_per_seq = 0, uint16_t* ple_conv_ckpt = nullptr,
                 const RaggedBatch* ragged = nullptr) {
  const ModelConfig& cfg = m.cfg;
  const uint16_t* trunk = trunk_in;
  uint16_t* next = trunk2;
  // Per-sequence 3D MRoPE table slice [3, max_len] (the pooled table is
  // [max_seq, 3, max_len]; seq_id selects this sequence's rows).
  const int* seq_rope_pos =
      m.d_rope_pos + static_cast<size_t>(seq_id) * 3 * cfg.max_len;
  // DIAG (Q4T_DIAG=1): check for async CUDA errors after each layer to
  // localize illegal-memory-access sources during concurrency debugging.
  const bool diag = std::getenv("Q4T_DIAG") != nullptr;
  int lin_idx = 0;   // running linear-layer index (for checkpoint offsets)
  int ple_idx = 0;   // running PLE-layer index (for checkpoint offsets)
  // Checkpoint layout is pooled over max_seq: [num_layers, max_seq, num_ckpt,
  // elems]. The kernels index the per-layer slice by the POOLED seq id (multi
  // seq, d_seq_id != null: pass the layer base, seq_off = 0) or by the single
  // sequence's slice (single seq: seq_off = seq_id). With max_seq == 1 this
  // degenerates to the legacy [num_layers, num_ckpt, elems] layout.
  const size_t max_seq = static_cast<size_t>(cfg.max_seq);
  const size_t seq_off = d_seq_id ? 0 : static_cast<size_t>(seq_id);
  for (int l = 0; l < cfg.num_layers; ++l) {
    if (diag) {
      const cudaError_t e = cudaGetLastError();
      if (e != cudaSuccess) {
        std::fprintf(stderr,
                     "[q4t][diag] pre-layer %d (seq_id=%d T=%d): %s\n", l,
                     seq_id, T, cudaGetErrorString(e));
        return Status::Fail(std::string("diag pre-layer ") +
                            std::to_string(l) + ": " + cudaGetErrorString(e));
      }
    }
    const uint16_t* ple_emb = nullptr;
    if (m.layers[l].has_ple) {
      Status s =
          m.ple_emb->Gather(ids64, T, hist, m.d_ple_emb, stream);
      if (!s.ok()) return s;
      // Apply the per-table weight_scale (SGLang: embeddings *= weight_scale).
      const int total = T * static_cast<int>(m.ple_emb->ple_embed_dim());
      ScaleBf16Kernel<<<(total + kBlock - 1) / kBlock, kBlock, 0, stream>>>(
          m.d_ple_emb, total, m.ple_weight_scale);
      if (cudaGetLastError() != cudaSuccess) {
        return Status::Fail("ple scale launch");
      }
      ple_emb = m.d_ple_emb;
    }
    // Per-linear-layer checkpoint slices (only when saving for MTP verify).
    // Pooled layout [num_lin, max_seq, num_ckpt, elems]: the per-layer slice
    // starts at (lin_idx * max_seq + seq_off) * num_ckpt * elems.
    float* layer_ssm_ckpt = nullptr;
    uint16_t* layer_conv_ckpt = nullptr;
    uint16_t* layer_ple_conv_ckpt = nullptr;
    if (ssm_ckpt && !m.layers[l].is_full_attention) {
      const auto& lin = m.layers[l].linear;
      const size_t ssm_elems =
          static_cast<size_t>(lin.nv) * lin.kd * lin.vd;
      const size_t conv_elems =
          static_cast<size_t>(lin.in_qkv()) * (lin.conv_k - 1);
      layer_ssm_ckpt = ssm_ckpt +
                       (static_cast<size_t>(lin_idx) * max_seq + seq_off) *
                           num_ckpt * ssm_elems;
      layer_conv_ckpt = conv_ckpt +
                        (static_cast<size_t>(lin_idx) * max_seq + seq_off) *
                            num_ckpt * conv_elems;
    }
    // PLE conv checkpoint slice (the PLE short-conv is an in-place recurrence
    // too; a partial accept must restore it — see ModelRestoreCheckpoint).
    // Uses the SEPARATE d_verify_ple_conv_ckpt buffer ([num_ple, max_seq,
    // cap, ple_elems]), not the linear conv buffer.
    if (ple_conv_ckpt && m.layers[l].has_ple) {
      const size_t ple_elems = static_cast<size_t>(m.layers[l].hc_dim) *
                               (m.layers[l].ple.conv_kernel - 1) *
                               m.layers[l].ple.conv_dilation;
      layer_ple_conv_ckpt = ple_conv_ckpt +
                            (static_cast<size_t>(ple_idx) * max_seq + seq_off) *
                                num_ckpt * ple_elems;
    }
    Status s = DecoderLayerForward(m.layers[l], trunk, ple_emb, next,
                                   m.d_positions, seq_rope_pos, T, m.d_ws,
                                   m.ws_bytes, stream, layer_ssm_ckpt,
                                   layer_conv_ckpt, num_ckpt, seq_id, d_seq_id,
                                   m.d_rope_pos, tokens_per_seq,
                                   layer_ple_conv_ckpt, ragged);
    if (!s.ok()) return s;
    if (!m.layers[l].is_full_attention) lin_idx++;
    if (m.layers[l].has_ple) ple_idx++;
    if (diag) {
      const cudaError_t e = cudaGetLastError();
      if (e != cudaSuccess) {
        std::fprintf(stderr,
                     "[q4t][diag] post-layer %d (%s, seq_id=%d T=%d): %s\n", l,
                     m.layers[l].is_full_attention ? "full" : "linear", seq_id,
                     T, cudaGetErrorString(e));
        return Status::Fail(std::string("diag post-layer ") +
                            std::to_string(l) + ": " + cudaGetErrorString(e));
      }
    }
    const uint16_t* t = trunk;
    trunk = next;
    next = const_cast<uint16_t*>(t);
  }
  // MTP scheme A hook: expose the pre-final-mixer multi stream [T, hc*hs]
  // (the trunk after the last decoder layer, before hyper_connection_mixer).
  // Device-to-device copy; the caller owns `trunk_out` (>= T * hc_dim bytes).
  if (trunk_out) {
    const size_t bytes = static_cast<size_t>(T) * m.hc_dim() * sizeof(uint16_t);
    if (cudaMemcpyAsync(trunk_out, trunk, bytes, cudaMemcpyDeviceToDevice,
                        stream) != cudaSuccess) {
      return Status::Fail("RunLayers: trunk_out D2D copy");
    }
  }
  // logits == nullptr: the caller only wants the trunk (e.g. the MTP draft
  // extend's hidden gather) and skips the lm_head GEMM. HeadForward would
  // otherwise pass a null output pointer to Bf16Gemm (CUBLAS_STATUS_INVALID_
  // VALUE).
  if (!logits) return Status();
  return HeadForward(m.head, trunk, logits, T, m.d_ws, m.ws_bytes, stream);
}

// Reset all per-layer persistent state (linear SSM/conv, full KV/indexer)
// to zero — prepare to process a fresh sequence from the start.
// const: ResetState only touches device memory, not the object.
Status ResetAllLayers(const Model& m, cudaStream_t stream, int seq_id = 0) {
  for (const auto& l : m.layers) l.ResetState(seq_id, stream);
  return Status();
}

// Build the 3D MRoPE position table (host) for a prefill, mirroring
// transformers qwen4_exp get_rope_index + get_vision_position_ids.
//
// The table has 3 rows (t, h, w) of length T, stored row-major into
// `rope_pos` (device [3, max_len]). For pure text the three rows are all the
// logical position 0..T-1 (delta = 0), so partial MRoPE reduces to standard
// RoPE. For multimodal prompts, each image/video placeholder block (grid
// (t, h, w) in PATCH units, in `vision->grids`, in prompt position order)
// contributes t*(h/m)*(w/m) tokens whose merged index k maps to
//   t_coord = k / ((h/m)*(w/m)),  h_coord = (k % ...) / (w/m),  w_coord = k % (w/m)
// and the three rows take (t_coord + clock, h_coord + clock, w_coord + clock).
// The text clock advances by `len` over a text run and by max(h, w) / m over a
// vision block (NOT the token count) — this is what creates the rope delta.
// Returns the mrope_position_delta (decode rope row = logical + delta).
int BuildRopePositions(const int32_t* input_ids, int T, int m,
                       int img_id, int vid_id, int max_len,
                       const std::vector<std::array<int, 3>>& grids,
                       std::vector<int>* rope_pos) {
  // Layout [3, max_len]: row r at ABSOLUTE position p is rope_pos[r*max_len+p].
  // The RoPE kernels index by positions[t] (== t in prefill, == absolute pos
  // in decode), so the table must be addressable by absolute position.
  rope_pos->assign(3 * max_len, 0);
  int clock = 0;  // text clock (current_pos in the reference)
  int gi = 0;  // grid index (in prompt position order)
  int t = 0;
  int maxv = 0;
  while (t < T) {
    const int32_t id = input_ids[t];
    if (id != img_id && id != vid_id) {
      // Text run: all three rows = clock + offset.
      int run = 0;
      while (t + run < T) {
        const int32_t d = input_ids[t + run];
        if (d == img_id || d == vid_id) break;
        ++run;
      }
      for (int j = 0; j < run; ++j) {
        const int v = clock + j;
        (*rope_pos)[t + j] = v;
        (*rope_pos)[max_len + (t + j)] = v;
        (*rope_pos)[2 * max_len + (t + j)] = v;
        if (v > maxv) maxv = v;
      }
      clock += run;
      t += run;
      continue;
    }
    // Vision block: consume the next grid.
    if (gi >= static_cast<int>(grids.size())) {
      // No grid for a placeholder: fall back to the logical position for this
      // token (defensive; the caller should always supply matching grids).
      const int v = t;
      (*rope_pos)[t] = v;
      (*rope_pos)[max_len + t] = v;
      (*rope_pos)[2 * max_len + t] = v;
      if (v > maxv) maxv = v;
      ++t;
      continue;
    }
    const int gt = grids[gi][0];  // temporal (patch groups)
    const int gh = grids[gi][1];  // height (patches)
    const int gw = grids[gi][2];  // width (patches)
    const int mh = gh / m, mw = gw / m;  // merged grid
    const int per_frame = mh * mw;
    const int n = gt * per_frame;  // merged tokens in this block
    for (int k = 0; k < n && t + k < T; ++k) {
      const int tc = k / per_frame;
      const int rem = k % per_frame;
      const int hc = rem / mw;
      const int wc = rem % mw;
      const int idx = t + k;
      (*rope_pos)[idx] = clock + tc;
      (*rope_pos)[max_len + idx] = clock + hc;
      (*rope_pos)[2 * max_len + idx] = clock + wc;
      const int v = clock + tc;
      if (v > maxv) maxv = v;
    }
    clock += (std::max(gh, gw) / m);
    t += n;
    ++gi;
  }
  return maxv + 1 - T;
}

// Prefill without reset: assumes per-layer state is already at the sequence
// start (call ResetAllLayers / ModelBeginSequence first). positions = 0..T-1.
// `trunk_out` (MTP scheme A hook) is forwarded to RunLayers. `vision` (optional)
// replaces image-token embeddings with vision features before ExpandTrunk.
Status RunPrefill(const Model& m, const int32_t* input_ids, int T,
                  uint16_t* logits, cudaStream_t stream,
                  uint16_t* trunk_out = nullptr,
                  const VisionFeatures* vision = nullptr, int seq_id = 0) {
  const ModelConfig& cfg = m.cfg;
  // 1. ids + positions.
  if (cudaMemcpyAsync(m.d_ids, input_ids, T * sizeof(int32_t),
                      cudaMemcpyHostToDevice, stream) != cudaSuccess) {
    return Status::Fail("H2D ids");
  }
  // PLE gather takes int64 token ids; build a host int64 copy.
  std::vector<int64_t> ids64(input_ids, input_ids + T);
  std::vector<int> positions(T);
  for (int t = 0; t < T; ++t) positions[t] = t;
  if (cudaMemcpyAsync(m.d_positions, positions.data(), T * sizeof(int),
                      cudaMemcpyHostToDevice, stream) != cudaSuccess) {
    return Status::Fail("H2D positions");
  }
  // 1b. 3D MRoPE table [3, max_len] (t, h, w) for the RoPE angles, addressed by
  //     ABSOLUTE position (rope_pos[r*max_len+p]). Pure text -> three identical
  //     rows (== logical), delta 0. Multimodal -> vision blocks carry their grid
  //     coordinates and the text clock advances by max(h, w) / merge over each
  //     block, producing a nonzero delta. The full table is H2D'd so the decode
  //     path (and the compressed-key builder) can read any absolute position.
  {
    const std::vector<std::array<int, 3>>* grids =
        (vision && !vision->grids.empty()) ? &vision->grids : nullptr;
    std::vector<int> rope_pos;
    m.rope_delta[seq_id] = BuildRopePositions(
        input_ids, T, cfg.spatial_merge_size, cfg.image_token_id,
        cfg.video_token_id, cfg.max_len,
        grids ? *grids : std::vector<std::array<int, 3>>{}, &rope_pos);
    // Write into this sequence's [3, max_len] slice of the pooled table.
    if (cudaMemcpyAsync(m.d_rope_pos + static_cast<size_t>(seq_id) * 3 * cfg.max_len,
                        rope_pos.data(), rope_pos.size() * sizeof(int),
                        cudaMemcpyHostToDevice, stream) != cudaSuccess) {
      return Status::Fail("H2D rope_pos");
    }
  }

  // 2. emb.
  Status s = EmbedLookup(m.head, m.d_ids, m.d_emb, T, stream);
  if (!s.ok()) return s;

  // 2b. Multimodal injection: replace image/video-token embeddings with vision
  //     features (vllm `_merge_multimodal_embeddings`). Feature rows are
  //     consumed in PROMPT POSITION ORDER (left-to-right): the i-th
  //     image_token_id or video_token_id occurrence (in sequence order) takes
  //     feature row i. The caller must lay out `vision->device` in that same
  //     left-to-right position order (interleaved images and videos allowed).
  if (vision && vision->num_tokens > 0) {
    std::vector<int> mm_pos;
    mm_pos.reserve(static_cast<size_t>(vision->num_tokens));
    for (int t = 0; t < T; ++t) {
      if (input_ids[t] == cfg.image_token_id ||
          input_ids[t] == cfg.video_token_id) {
        mm_pos.push_back(t);
      }
    }
    if (static_cast<int>(mm_pos.size()) != vision->num_tokens) {
      return Status::Fail("vision: multimodal-token count mismatch " +
                           std::to_string(mm_pos.size()) + " vs " +
                           std::to_string(vision->num_tokens));
    }
    if (cudaMemcpyAsync(m.d_img_pos, mm_pos.data(),
                        mm_pos.size() * sizeof(int), cudaMemcpyHostToDevice,
                        stream) != cudaSuccess) {
      return Status::Fail("H2D img_pos");
    }
    const int total = vision->num_tokens * cfg.hs;
    const int grid = (total + kBlock - 1) / kBlock;
    InjectVisionKernel<<<grid, kBlock, 0, stream>>>(vision->device, m.d_img_pos,
                                                    m.d_emb, cfg.hs,
                                                    vision->num_tokens);
  }

  // 3. trunk.
  s = ExpandTrunk(m.head, m.d_emb, m.d_trunk, T, stream);
  if (!s.ok()) return s;

  // 4. Build the PLE ngram history (host): for token t, the ngram_size-1
  //    preceding tokens oldest->newest, EOS-filled before the sequence start.
  std::vector<int64_t> hist;
  if (m.ple_emb) {
    const int hist_w = m.ple_hash.ngram_size - 1;
    hist.resize(static_cast<size_t>(T) * hist_w);
    for (int t = 0; t < T; ++t) {
      for (int j = 0; j < hist_w; ++j) {
        const int src = t - (hist_w - j);  // oldest first
        hist[static_cast<size_t>(t) * hist_w + j] =
            (src >= 0) ? static_cast<int64_t>(input_ids[src])
                       : cfg.eos_token_id;
      }
    }
  }

  // 5. Layer loop + head.
  return RunLayers(m, m.d_trunk, m.d_trunk2, ids64.data(), hist.data(), T,
                   logits, stream, trunk_out, nullptr, nullptr, 0, seq_id);
}

Status ModelForward(const Model& m, const int32_t* input_ids, int T,
                    uint16_t* logits, cudaStream_t stream,
                    const VisionFeatures* vision, int seq_id) {
  const ModelConfig& cfg = m.cfg;
  if (T <= 0) return Status();
  if (T > cfg.max_prefill) {
    return Status::Fail("ModelForward: T exceeds max_prefill");
  }
  // Prefill of a fresh sequence starts from empty per-layer state (linear
  // SSM/conv, full KV/indexer). Reset so repeated calls are deterministic.
  Status s = ResetAllLayers(m, stream, seq_id);
  if (!s.ok()) return s;
  return RunPrefill(m, input_ids, T, logits, stream, nullptr, vision, seq_id);
}

Status ModelDecodeStep(const Model& m, int32_t token_id, int position,
                       const int32_t* history, uint16_t* logits,
                       cudaStream_t stream, uint16_t* trunk_out, int seq_id) {
  const ModelConfig& cfg = m.cfg;
  if (position < 0) return Status::Fail("ModelDecodeStep: position < 0");
  if (position >= cfg.max_len) {
    return Status::Fail("ModelDecodeStep: position exceeds max_len");
  }

  // Per-layer state is NOT reset: it continues from the prior steps.
  // 1. ids + positions (single token at its absolute position).
  if (cudaMemcpyAsync(m.d_ids, &token_id, sizeof(int32_t),
                      cudaMemcpyHostToDevice, stream) != cudaSuccess) {
    return Status::Fail("H2D id");
  }
  const int pos = position;
  if (cudaMemcpyAsync(m.d_positions, &pos, sizeof(int), cudaMemcpyHostToDevice,
                      stream) != cudaSuccess) {
    return Status::Fail("H2D position");
  }
  // 1b. 3D MRoPE for this decode token: all three rows = logical + delta
  //     (the rope delta computed at prefill; 0 for pure text). The RoPE
  //     kernels index rope_pos by the ABSOLUTE position: row r at position p
  //     is rope_pos[r * max_len + p] (layout [3, max_len]). Write the three
  //     rows at their strided offsets.
  {
    const int rp = pos + m.rope_delta[seq_id];
    const size_t ml = static_cast<size_t>(cfg.max_len);
    int* seq_rope = m.d_rope_pos + static_cast<size_t>(seq_id) * 3 * ml;
    for (int r = 0; r < 3; ++r) {
      if (cudaMemcpyAsync(seq_rope + r * ml + pos, &rp, sizeof(int),
                          cudaMemcpyHostToDevice, stream) != cudaSuccess) {
        return Status::Fail("H2D rope_pos");
      }
    }
  }

  // 2-3. emb + trunk.
  Status s = EmbedLookup(m.head, m.d_ids, m.d_emb, 1, stream);
  if (!s.ok()) return s;
  s = ExpandTrunk(m.head, m.d_emb, m.d_trunk, 1, stream);
  if (!s.ok()) return s;

  // 4. PLE ngram history for this token: the ngram_size-1 preceding tokens
  //    (from `history`, oldest->newest), EOS-filled before the start.
  std::vector<int64_t> ids64(1, static_cast<int64_t>(token_id));
  std::vector<int64_t> hist;
  if (m.ple_emb) {
    const int hist_w = m.ple_hash.ngram_size - 1;
    hist.resize(hist_w);
    for (int j = 0; j < hist_w; ++j) {
      const int src = position - (hist_w - j);  // oldest first
      hist[j] = (src >= 0) ? static_cast<int64_t>(history[src])
                           : cfg.eos_token_id;
    }
  }

  // 5. Layer loop + head.
  return RunLayers(m, m.d_trunk, m.d_trunk2, ids64.data(), hist.data(), 1,
                   logits, stream, trunk_out, nullptr, nullptr, 0, seq_id);
}

// Batched decode over T tokens at absolute positions [base..base+T-1] WITHOUT
// resetting per-layer state (continues from the current KV/SSM/conv). Returns
// [T, vocab] logits (and optionally [T, hc*hs] trunk_out). Used by MTP to
// verify k+1 speculative tokens in a single bandwidth-bound forward instead of
// k+1 separate T=1 decodes. `history` (length `history_len`) supplies the PLE
// n-gram context for positions before `base`; the in-batch prefix supplies the
// rest.
Status ModelDecodeBatch(const Model& m, const int32_t* input_ids, int T,
                        int base_position, const int32_t* history,
                        int history_len, uint16_t* logits, cudaStream_t stream,
                        uint16_t* trunk_out, bool save_checkpoints,
                        int seq_id) {
  const ModelConfig& cfg = m.cfg;
  if (T <= 0) return Status::Fail("ModelDecodeBatch: T must be > 0");
  if (T > cfg.max_prefill)
    return Status::Fail("ModelDecodeBatch: T exceeds max_prefill");
  if (base_position < 0 || base_position + T > cfg.max_len)
    return Status::Fail("ModelDecodeBatch: position range exceeds max_len");

  if (cudaMemcpyAsync(m.d_ids, input_ids, T * sizeof(int32_t),
                      cudaMemcpyHostToDevice, stream) != cudaSuccess)
    return Status::Fail("H2D ids");
  std::vector<int64_t> ids64(input_ids, input_ids + T);
  std::vector<int> positions(T);
  for (int t = 0; t < T; ++t) positions[t] = base_position + t;
  if (cudaMemcpyAsync(m.d_positions, positions.data(), T * sizeof(int),
                      cudaMemcpyHostToDevice, stream) != cudaSuccess)
    return Status::Fail("H2D positions");
  // 3D MRoPE for the decode batch (all text tokens): each row = logical +
  // delta (the rope delta from prefill; 0 for pure text). The RoPE kernels
  // index rope_pos by the ABSOLUTE position (positions[t] == base_position+t),
  // so write the three rows at their strided [3, max_len] offsets.
  //
  // All three rows carry the SAME value for a text token (t = h = w = logical
  // position + delta), so build the T values once and copy each of the 3 rows
  // as a SINGLE contiguous H2D into [base_position, base_position+T). The old
  // per-token 3xT tiny (4-byte) copies serialized ~3T H2D launches per call —
  // a 2048-token chunk meant 6144 launches, which dominated chunked-prefill
  // time at 262K (see LOG 2026-09-15). Bit-identical (same values, same
  // destination offsets), just batched.
  {
    const size_t ml = static_cast<size_t>(cfg.max_len);
    int* seq_rope = m.d_rope_pos + static_cast<size_t>(seq_id) * 3 * ml;
    std::vector<int> rope_pos(T);
    for (int t = 0; t < T; ++t) rope_pos[t] = base_position + t + m.rope_delta[seq_id];
    for (int r = 0; r < 3; ++r) {
      if (cudaMemcpyAsync(seq_rope + r * ml + base_position, rope_pos.data(),
                          T * sizeof(int), cudaMemcpyHostToDevice, stream) !=
          cudaSuccess)
        return Status::Fail("H2D rope_pos");
    }
  }

  Status s = EmbedLookup(m.head, m.d_ids, m.d_emb, T, stream);
  if (!s.ok()) return s;
  s = ExpandTrunk(m.head, m.d_emb, m.d_trunk, T, stream);
  if (!s.ok()) return s;

  // PLE n-gram history: token t at absolute (base+t); its context spans the
  // caller's `history` (positions < base) and the in-batch prefix.
  std::vector<int64_t> hist;
  if (m.ple_emb) {
    const int hist_w = m.ple_hash.ngram_size - 1;
    hist.resize(static_cast<size_t>(T) * hist_w);
    for (int t = 0; t < T; ++t) {
      const int abs = base_position + t;
      for (int j = 0; j < hist_w; ++j) {
        const int src = abs - (hist_w - j);  // oldest first
        int64_t tok;
        if (src < 0)
          tok = cfg.eos_token_id;
        else if (src < base_position)
          tok = (src < history_len) ? static_cast<int64_t>(history[src])
                                    : cfg.eos_token_id;
        else
          tok = static_cast<int64_t>(input_ids[src - base_position]);
        hist[static_cast<size_t>(t) * hist_w + j] = tok;
      }
    }
  }

  // Layer loop + head — NO reset, continues from the current per-layer state.
  // When save_checkpoints, capture per-token SSM/conv state for the first T-1
  // tokens (checkpoint[t] = state after token t) so an MTP partial accept can
  // restore checkpoint[accept_count] via D2D instead of re-running the forward.
  float* ssm_ckpt = save_checkpoints ? m.d_verify_ssm_ckpt : nullptr;
  uint16_t* conv_ckpt = save_checkpoints ? m.d_verify_conv_ckpt : nullptr;
  const int num_ckpt = save_checkpoints ? (T - 1) : 0;
  return RunLayers(m, m.d_trunk, m.d_trunk2, ids64.data(), hist.data(), T,
                   logits, stream, trunk_out, ssm_ckpt, conv_ckpt, num_ckpt,
                   seq_id);
}

// B2 continuous batching: decode ONE token for each of B sequences in a single
// packed forward (T = B). See the header for the argument contract. The GEMMs
// (HC/MoE/head/projections) are stateless and operate on the packed [B, ...]
// rows (weights read once); the recurrent state (linear SSM/conv, PLE conv,
// full KV/indexer, 3D MRoPE) is selected per-token via d_seq_id.
Status ModelDecodeBatchMulti(const Model& m, const int32_t* tokens,
                             const int* positions, const int* seq_ids,
                             const int32_t* history, int B, uint16_t* logits,
                             cudaStream_t stream, uint16_t* trunk_out) {
  const ModelConfig& cfg = m.cfg;
  if (B <= 0) return Status::Fail("ModelDecodeBatchMulti: B must be > 0");
  if (B > cfg.max_seq)
    return Status::Fail("ModelDecodeBatchMulti: B exceeds max_seq");
  for (int t = 0; t < B; ++t) {
    if (positions[t] < 0 || positions[t] >= cfg.max_len)
      return Status::Fail("ModelDecodeBatchMulti: position exceeds max_len");
    if (seq_ids[t] < 0 || seq_ids[t] >= cfg.max_seq)
      return Status::Fail("ModelDecodeBatchMulti: seq_id exceeds max_seq");
  }

  // 1. H2D the packed tokens / positions / per-token seq ids.
  if (cudaMemcpyAsync(m.d_ids, tokens, B * sizeof(int32_t),
                      cudaMemcpyHostToDevice, stream) != cudaSuccess)
    return Status::Fail("H2D tokens");
  if (cudaMemcpyAsync(m.d_positions, positions, B * sizeof(int),
                      cudaMemcpyHostToDevice, stream) != cudaSuccess)
    return Status::Fail("H2D positions");
  if (cudaMemcpyAsync(m.d_seq_id, seq_ids, B * sizeof(int),
                      cudaMemcpyHostToDevice, stream) != cudaSuccess)
    return Status::Fail("H2D seq_ids");

  std::vector<int64_t> ids64(tokens, tokens + B);

  // 2. PLE n-gram history: the caller supplies per-token context (oldest
  //    first, EOS-filled) in `history` (B * (ngram_size-1) int32), so it maps
  //    directly to the int64 hist RunLayers expects (one row per token).
  std::vector<int64_t> hist;
  if (m.ple_emb) {
    const int hist_w = m.ple_hash.ngram_size - 1;
    hist.resize(static_cast<size_t>(B) * hist_w);
    for (size_t i = 0; i < hist.size(); ++i)
      hist[i] = static_cast<int64_t>(history[i]);
  }

  // 3. emb + trunk (packed [B, ...]).
  Status s = EmbedLookup(m.head, m.d_ids, m.d_emb, B, stream);
  if (!s.ok()) return s;
  s = ExpandTrunk(m.head, m.d_emb, m.d_trunk, B, stream);
  if (!s.ok()) return s;

  // 4. Layer loop + head — NO reset, continues from each sequence's current
  //    per-layer state (selected per-token via d_seq_id).
  return RunLayers(m, m.d_trunk, m.d_trunk2, ids64.data(), hist.data(), B,
                   logits, stream, trunk_out, nullptr, nullptr, 0, 0,
                   m.d_seq_id);
}

// Phase 2 MTP multi-seq verify: feed `tokens_per_seq` tokens for EACH of B
// sequences in ONE packed forward (T_total = B * tokens_per_seq, sequence-
// major). See the header for the full contract. Unlike ModelDecodeBatch (one
// sequence), this packs B sequences so the GEMMs read the weights once for the
// whole speculative batch; the per-sequence recurrent state (linear SSM/conv,
// PLE conv, full KV/indexer, 3D MRoPE) is selected per-token via
// d_token_seq_id. Per-sequence per-token SSM/conv/PLE-conv checkpoints are
// saved (num_ckpt = tokens_per_seq - 1) so each sequence can independently
// roll back to its own accepted prefix.
Status ModelVerifyMulti(const Model& m, const int32_t* tokens,
                        const int* base_positions, const int* seq_ids,
                        const int32_t* history, int history_len, int B,
                        int tokens_per_seq, uint16_t* logits,
                        cudaStream_t stream, uint16_t* trunk_out) {
  const ModelConfig& cfg = m.cfg;
  const int T = tokens_per_seq;
  const int Ttot = B * T;
  if (B <= 0 || T <= 0)
    return Status::Fail("ModelVerifyMulti: B and T must be > 0");
  if (B > cfg.max_seq)
    return Status::Fail("ModelVerifyMulti: B exceeds max_seq");
  if (Ttot > cfg.max_prefill)
    return Status::Fail("ModelVerifyMulti: B*T exceeds max_prefill");
  for (int b = 0; b < B; ++b) {
    if (base_positions[b] < 0 || base_positions[b] + T > cfg.max_len)
      return Status::Fail("ModelVerifyMulti: position range exceeds max_len");
    if (seq_ids[b] < 0 || seq_ids[b] >= cfg.max_seq)
      return Status::Fail("ModelVerifyMulti: seq_id exceeds max_seq");
  }

  // 1. H2D the packed tokens / positions / per-token seq ids (sequence-major).
  if (cudaMemcpyAsync(m.d_ids, tokens, Ttot * sizeof(int32_t),
                      cudaMemcpyHostToDevice, stream) != cudaSuccess)
    return Status::Fail("H2D tokens");
  std::vector<int> positions(Ttot);
  std::vector<int> token_seq(Ttot);
  for (int b = 0; b < B; ++b) {
    for (int t = 0; t < T; ++t) {
      positions[static_cast<size_t>(b) * T + t] = base_positions[b] + t;
      token_seq[static_cast<size_t>(b) * T + t] = seq_ids[b];
    }
  }
  if (cudaMemcpyAsync(m.d_positions, positions.data(), Ttot * sizeof(int),
                      cudaMemcpyHostToDevice, stream) != cudaSuccess)
    return Status::Fail("H2D positions");
  if (cudaMemcpyAsync(m.d_token_seq_id, token_seq.data(), Ttot * sizeof(int),
                      cudaMemcpyHostToDevice, stream) != cudaSuccess)
    return Status::Fail("H2D token_seq");

  // 3D MRoPE per sequence (all text tokens): each row = logical + delta,
  // written at the strided [3, max_len] offsets for that sequence's slice.
  {
    const size_t ml = static_cast<size_t>(cfg.max_len);
    for (int b = 0; b < B; ++b) {
      int* seq_rope = m.d_rope_pos + static_cast<size_t>(seq_ids[b]) * 3 * ml;
      for (int t = 0; t < T; ++t) {
        const int p = base_positions[b] + t;
        const int rp = p + m.rope_delta[seq_ids[b]];
        for (int r = 0; r < 3; ++r) {
          if (cudaMemcpyAsync(seq_rope + r * ml + p, &rp, sizeof(int),
                              cudaMemcpyHostToDevice, stream) !=
              cudaSuccess)
            return Status::Fail("H2D rope_pos");
        }
      }
    }
  }

  std::vector<int64_t> ids64(tokens, tokens + Ttot);

  // PLE n-gram history: per sequence, token (b, t) at absolute
  // base_positions[b]+t; its context spans the sequence's `history` (positions
  // < base) and the in-batch prefix. Mirrors ModelDecodeBatch, per sequence.
  std::vector<int64_t> hist;
  if (m.ple_emb) {
    const int hist_w = m.ple_hash.ngram_size - 1;
    hist.resize(static_cast<size_t>(Ttot) * hist_w);
    for (int b = 0; b < B; ++b) {
      for (int t = 0; t < T; ++t) {
        const int abs = base_positions[b] + t;
        for (int j = 0; j < hist_w; ++j) {
          const int src = abs - (hist_w - j);  // oldest first
          int64_t tok;
          if (src < 0)
            tok = cfg.eos_token_id;
          else if (src < base_positions[b])
            tok = (src < history_len)
                      ? static_cast<int64_t>(
                            history[static_cast<size_t>(b) * history_len + src])
                      : cfg.eos_token_id;
          else
            tok = static_cast<int64_t>(
                tokens[static_cast<size_t>(b) * T + (src - base_positions[b])]);
          hist[static_cast<size_t>(b) * T * hist_w + t * hist_w + j] = tok;
        }
      }
    }
  }

  // emb + trunk (packed [B*T, ...]).
  Status s = EmbedLookup(m.head, m.d_ids, m.d_emb, Ttot, stream);
  if (!s.ok()) return s;
  s = ExpandTrunk(m.head, m.d_emb, m.d_trunk, Ttot, stream);
  if (!s.ok()) return s;

  // Layer loop + head — NO reset; per-token state via d_token_seq_id, and
  // per-sequence per-token SSM/conv/PLE-conv checkpoints for the first T-1
  // tokens so each sequence rolls back to its own accepted prefix.
  const int num_ckpt = T - 1;
  return RunLayers(m, m.d_trunk, m.d_trunk2, ids64.data(), hist.data(), Ttot,
                   logits, stream, trunk_out, m.d_verify_ssm_ckpt,
                   m.d_verify_conv_ckpt, num_ckpt, 0, m.d_token_seq_id, T,
                   m.d_verify_ple_conv_ckpt);
}

// Ragged batched prefill (Phase 2): prefill B FRESH sequences of variable
// length in ONE packed forward. `tokens` is the sequence-major concatenation
// of the B prompts (Ttot = sum(lens) rows), `lens[b]` the b-th prompt length,
// `seq_ids[b]` the pooled recurrent-state slice it fills. Each sequence's
// per-layer state is RESET first (fresh prefill from position 0). `logits`
// receives the packed [Ttot, vocab] logits (row seq_offset[b]+t = sequence b's
// token t). The projection / MoE / attention GEMMs read the dense weights ONCE
// for the whole batch, so B short prefills cost ~one weight sweep instead of B
// — the serve win when several requests prefill together. Pure text only (no
// vision); per-sequence rope_delta = 0.
Status ModelPrefillBatch(const Model& m, const int32_t* tokens,
                         const int* lens, const int* seq_ids, int B,
                         uint16_t* logits, cudaStream_t stream) {
  const ModelConfig& cfg = m.cfg;
  if (B <= 0) return Status::Fail("ModelPrefillBatch: B must be > 0");
  if (B > cfg.max_seq)
    return Status::Fail("ModelPrefillBatch: B exceeds max_seq");

  // cu_seqlens + per-sequence validation.
  std::vector<int> seq_offset(B + 1);
  seq_offset[0] = 0;
  for (int b = 0; b < B; ++b) {
    if (lens[b] <= 0) return Status::Fail("ModelPrefillBatch: len must be > 0");
    if (lens[b] > cfg.max_len)
      return Status::Fail("ModelPrefillBatch: len exceeds max_len");
    if (seq_ids[b] < 0 || seq_ids[b] >= cfg.max_seq)
      return Status::Fail("ModelPrefillBatch: seq_id exceeds max_seq");
    seq_offset[b + 1] = seq_offset[b] + lens[b];
  }
  const int Ttot = seq_offset[B];
  if (Ttot > cfg.max_prefill)
    return Status::Fail("ModelPrefillBatch: Ttot exceeds max_prefill");

  // Fresh prefill: reset each sequence's per-layer state (per-sequence slice).
  for (int b = 0; b < B; ++b) ResetAllLayers(m, stream, seq_ids[b]);

  // Per-token positions (local 0..len-1 for a fresh sequence), pooled seq id,
  // local position (the ragged token_local array).
  std::vector<int> positions(Ttot);
  std::vector<int> token_seq(Ttot);
  std::vector<int> token_local(Ttot);
  for (int b = 0; b < B; ++b) {
    const int off = seq_offset[b];
    for (int t = 0; t < lens[b]; ++t) {
      positions[off + t] = t;
      token_seq[off + t] = seq_ids[b];
      token_local[off + t] = t;
    }
    m.rope_delta[seq_ids[b]] = 0;  // pure text
  }

  // H2D packed tokens / positions / per-token seq id / ragged descriptor.
  if (cudaMemcpyAsync(m.d_ids, tokens, Ttot * sizeof(int32_t),
                      cudaMemcpyHostToDevice, stream) != cudaSuccess)
    return Status::Fail("H2D tokens");
  if (cudaMemcpyAsync(m.d_positions, positions.data(), Ttot * sizeof(int),
                      cudaMemcpyHostToDevice, stream) != cudaSuccess)
    return Status::Fail("H2D positions");
  if (cudaMemcpyAsync(m.d_token_seq_id, token_seq.data(), Ttot * sizeof(int),
                      cudaMemcpyHostToDevice, stream) != cudaSuccess)
    return Status::Fail("H2D token_seq");
  if (cudaMemcpyAsync(m.d_ragged_seq_offset, seq_offset.data(),
                      (B + 1) * sizeof(int), cudaMemcpyHostToDevice, stream) !=
      cudaSuccess)
    return Status::Fail("H2D seq_offset");
  if (cudaMemcpyAsync(m.d_ragged_token_local, token_local.data(),
                      Ttot * sizeof(int), cudaMemcpyHostToDevice, stream) !=
      cudaSuccess)
    return Status::Fail("H2D token_local");

  // 3D MRoPE: pure text, each sequence's three rows = local position 0..len-1
  // (delta 0). Write into the pooled table at the sequence's [3, max_len] slice.
  {
    const size_t ml = static_cast<size_t>(cfg.max_len);
    std::vector<int> rope_row;
    for (int b = 0; b < B; ++b) {
      int* seq_rope = m.d_rope_pos + static_cast<size_t>(seq_ids[b]) * 3 * ml;
      rope_row.resize(lens[b]);
      for (int t = 0; t < lens[b]; ++t) rope_row[t] = t;
      for (int r = 0; r < 3; ++r) {
        if (cudaMemcpyAsync(seq_rope + r * ml, rope_row.data(),
                            lens[b] * sizeof(int), cudaMemcpyHostToDevice,
                            stream) != cudaSuccess)
          return Status::Fail("H2D rope_pos");
      }
    }
  }

  std::vector<int64_t> ids64(tokens, tokens + Ttot);

  // PLE n-gram history: per sequence, per token, the ngram_size-1 preceding
  // tokens (oldest first, EOS-filled before the sequence start), local to the
  // sequence (fresh prefill, no cross-sequence context).
  std::vector<int64_t> hist;
  if (m.ple_emb) {
    const int hist_w = m.ple_hash.ngram_size - 1;
    hist.resize(static_cast<size_t>(Ttot) * hist_w);
    for (int b = 0; b < B; ++b) {
      const int off = seq_offset[b];
      for (int t = 0; t < lens[b]; ++t) {
        for (int j = 0; j < hist_w; ++j) {
          const int src = t - (hist_w - j);  // oldest first, local position
          hist[static_cast<size_t>(off + t) * hist_w + j] =
              (src >= 0) ? static_cast<int64_t>(tokens[off + src])
                         : cfg.eos_token_id;
        }
      }
    }
  }

  // emb + trunk (packed [Ttot, ...]).
  Status s = EmbedLookup(m.head, m.d_ids, m.d_emb, Ttot, stream);
  if (!s.ok()) return s;
  s = ExpandTrunk(m.head, m.d_emb, m.d_trunk, Ttot, stream);
  if (!s.ok()) return s;

  // Layer loop + head. Per-token pooled state via d_token_seq_id; variable
  // lengths via the ragged descriptor (cu_seqlens + token_local). No
  // checkpoints (fresh prefill, not a speculative verify).
  RaggedBatch ragged;
  ragged.seq_offset = m.d_ragged_seq_offset;
  ragged.token_local = m.d_ragged_token_local;
  ragged.B = B;
  return RunLayers(m, m.d_trunk, m.d_trunk2, ids64.data(), hist.data(), Ttot,
                   logits, stream, nullptr, nullptr, nullptr, 0, 0,
                   m.d_token_seq_id, 0, nullptr, &ragged);
}

// Fused mixed prefill+decode forward (see model.h). A generalization of
// ModelPrefillBatch: each sequence may start fresh (base_position 0 -> reset)
// or continue (base_position > 0 -> keep state), so fresh-prefill chunks and
// continuing-decode tokens share ONE weight sweep. The ragged causal path
// (cu_seqlens + token_local) drives the per-sequence linear/PLE chains for both
// kinds; the reset and the absolute base position are the only per-seq knobs.
Status ModelMixedBatch(const Model& m, const int32_t* tokens, const int* lens,
                       const int* seq_ids, const int* base_positions,
                       const int32_t* prior_history, int B, uint16_t* logits,
                       cudaStream_t stream) {
  const ModelConfig& cfg = m.cfg;
  if (B <= 0) return Status::Fail("ModelMixedBatch: B must be > 0");
  if (B > cfg.max_seq)
    return Status::Fail("ModelMixedBatch: B exceeds max_seq");

  std::vector<int> seq_offset(B + 1);
  seq_offset[0] = 0;
  for (int b = 0; b < B; ++b) {
    if (lens[b] <= 0) return Status::Fail("ModelMixedBatch: len must be > 0");
    if (base_positions[b] < 0 || base_positions[b] + lens[b] > cfg.max_len)
      return Status::Fail("ModelMixedBatch: position range exceeds max_len");
    if (seq_ids[b] < 0 || seq_ids[b] >= cfg.max_seq)
      return Status::Fail("ModelMixedBatch: seq_id exceeds max_seq");
    seq_offset[b + 1] = seq_offset[b] + lens[b];
  }
  const int Ttot = seq_offset[B];
  if (Ttot > cfg.max_prefill)
    return Status::Fail("ModelMixedBatch: Ttot exceeds max_prefill");

  // Fresh sequences (base_position == 0) reset their per-layer state; continue
  // sequences keep it — this is what lets fresh-prefill chunks and continuing-
  // decode tokens share one forward.
  for (int b = 0; b < B; ++b)
    if (base_positions[b] == 0) ResetAllLayers(m, stream, seq_ids[b]);

  std::vector<int> positions(Ttot);
  std::vector<int> token_seq(Ttot);
  std::vector<int> token_local(Ttot);
  for (int b = 0; b < B; ++b) {
    const int off = seq_offset[b];
    const int base = base_positions[b];
    for (int t = 0; t < lens[b]; ++t) {
      positions[off + t] = base + t;  // absolute position
      token_seq[off + t] = seq_ids[b];
      token_local[off + t] = t;  // local within this chunk (drives causal chain)
    }
    if (base == 0) m.rope_delta[seq_ids[b]] = 0;  // fresh text sequence
  }

  if (cudaMemcpyAsync(m.d_ids, tokens, Ttot * sizeof(int32_t),
                      cudaMemcpyHostToDevice, stream) != cudaSuccess)
    return Status::Fail("H2D tokens");
  if (cudaMemcpyAsync(m.d_positions, positions.data(), Ttot * sizeof(int),
                      cudaMemcpyHostToDevice, stream) != cudaSuccess)
    return Status::Fail("H2D positions");
  if (cudaMemcpyAsync(m.d_token_seq_id, token_seq.data(), Ttot * sizeof(int),
                      cudaMemcpyHostToDevice, stream) != cudaSuccess)
    return Status::Fail("H2D token_seq");
  if (cudaMemcpyAsync(m.d_ragged_seq_offset, seq_offset.data(),
                      (B + 1) * sizeof(int), cudaMemcpyHostToDevice, stream) !=
      cudaSuccess)
    return Status::Fail("H2D seq_offset");
  if (cudaMemcpyAsync(m.d_ragged_token_local, token_local.data(),
                      Ttot * sizeof(int), cudaMemcpyHostToDevice, stream) !=
      cudaSuccess)
    return Status::Fail("H2D token_local");

  // 3D MRoPE (pure text): each sequence's three rows = absolute position
  // base+t, written into the [base, base+len) slice of its [3, max_len] table.
  {
    const size_t ml = static_cast<size_t>(cfg.max_len);
    std::vector<int> rope_row;
    for (int b = 0; b < B; ++b) {
      int* seq_rope = m.d_rope_pos + static_cast<size_t>(seq_ids[b]) * 3 * ml;
      const int base = base_positions[b];
      rope_row.resize(lens[b]);
      for (int t = 0; t < lens[b]; ++t) rope_row[t] = base + t;
      for (int r = 0; r < 3; ++r) {
        if (cudaMemcpyAsync(seq_rope + r * ml + base, rope_row.data(),
                            lens[b] * sizeof(int), cudaMemcpyHostToDevice,
                            stream) != cudaSuccess)
          return Status::Fail("H2D rope_pos");
      }
    }
  }

  std::vector<int64_t> ids64(tokens, tokens + Ttot);

  // PLE n-gram history: for token (off+t) the w preceding tokens (oldest
  // first). In-chunk part (j+t >= w) from `tokens`; before-chunk part from the
  // caller's per-seq `prior_history` (EOS-filled for fresh sequences, so a
  // fresh sequence reproduces ModelPrefillBatch exactly).
  std::vector<int64_t> hist;
  if (m.ple_emb) {
    const int w = m.ple_hash.ngram_size - 1;
    hist.resize(static_cast<size_t>(Ttot) * w);
    for (int b = 0; b < B; ++b) {
      const int off = seq_offset[b];
      const int32_t* ph = prior_history + static_cast<size_t>(b) * w;
      for (int t = 0; t < lens[b]; ++t) {
        for (int j = 0; j < w; ++j) {
          const int src = j + t - w;  // in-chunk local index if >= 0
          hist[static_cast<size_t>(off + t) * w + j] =
              (src >= 0) ? static_cast<int64_t>(tokens[off + src])
                         : static_cast<int64_t>(ph[j + t]);
        }
      }
    }
  }

  Status s = EmbedLookup(m.head, m.d_ids, m.d_emb, Ttot, stream);
  if (!s.ok()) return s;
  s = ExpandTrunk(m.head, m.d_emb, m.d_trunk, Ttot, stream);
  if (!s.ok()) return s;

  RaggedBatch ragged2;
  ragged2.seq_offset = m.d_ragged_seq_offset;
  ragged2.token_local = m.d_ragged_token_local;
  ragged2.B = B;
  return RunLayers(m, m.d_trunk, m.d_trunk2, ids64.data(), hist.data(), Ttot,
                   logits, stream, nullptr, nullptr, nullptr, 0, 0,
                   m.d_token_seq_id, 0, nullptr, &ragged2);
}


// the same dims, which they do for this architecture).
namespace {
struct LinCkptDims {
  size_t ssm_elems = 0;
  size_t conv_elems = 0;
  size_t ple_conv_elems = 0;  // PLE short-conv state (0 if no PLE layer)
  int num_lin = 0;
  int num_ple = 0;
};
LinCkptDims CollectLinCkptDims(const Model& m) {
  LinCkptDims d;
  for (const auto& l : m.layers) {
    if (l.is_full_attention) continue;
    if (d.num_lin == 0) {
      d.ssm_elems = static_cast<size_t>(l.linear.nv) * l.linear.kd * l.linear.vd;
      d.conv_elems =
          static_cast<size_t>(l.linear.in_qkv()) * (l.linear.conv_k - 1);
    }
    d.num_lin++;
    if (l.has_ple) {
      if (d.num_ple == 0) {
        d.ple_conv_elems = static_cast<size_t>(l.hc_dim) *
                           (l.ple.conv_kernel - 1) * l.ple.conv_dilation;
      }
      d.num_ple++;
    }
  }
  return d;
}
}  // namespace

Status ModelReserveVerifyCheckpoints(Model& m, int num_ckpt) {
  if (num_ckpt <= m.verify_ckpt_cap) return Status();
  const LinCkptDims d = CollectLinCkptDims(m);
  if (d.num_lin == 0) return Status();  // no linear layers (nothing to save)
  if (m.d_verify_ssm_ckpt) cudaFree(m.d_verify_ssm_ckpt);
  if (m.d_verify_conv_ckpt) cudaFree(m.d_verify_conv_ckpt);
  if (m.d_verify_ple_conv_ckpt) cudaFree(m.d_verify_ple_conv_ckpt);
  m.d_verify_ssm_ckpt = nullptr;
  m.d_verify_conv_ckpt = nullptr;
  m.d_verify_ple_conv_ckpt = nullptr;
  // Pooled over max_seq (Phase 2 MTP multi-seq verify): [num_layers, max_seq,
  // cap, elems]. max_seq == 1 degenerates to the legacy [num_layers, cap,
  // elems] layout (bit-identical single-seq path).
  const size_t max_seq = static_cast<size_t>(m.cfg.max_seq);
  const size_t ssm_bytes = static_cast<size_t>(d.num_lin) * max_seq * num_ckpt *
                           d.ssm_elems * sizeof(float);
  const size_t conv_bytes = static_cast<size_t>(d.num_lin) * max_seq * num_ckpt *
                            d.conv_elems * sizeof(uint16_t);
  const size_t ple_bytes = static_cast<size_t>(d.num_ple) * max_seq * num_ckpt *
                           d.ple_conv_elems * sizeof(uint16_t);
  if (cudaMalloc(reinterpret_cast<void**>(&m.d_verify_ssm_ckpt), ssm_bytes) !=
      cudaSuccess)
    return Status::Fail("ModelReserveVerifyCheckpoints: cudaMalloc ssm");
  if (cudaMalloc(reinterpret_cast<void**>(&m.d_verify_conv_ckpt), conv_bytes) !=
      cudaSuccess)
    return Status::Fail("ModelReserveVerifyCheckpoints: cudaMalloc conv");
  if (d.num_ple > 0 &&
      cudaMalloc(reinterpret_cast<void**>(&m.d_verify_ple_conv_ckpt),
                 ple_bytes) != cudaSuccess)
    return Status::Fail("ModelReserveVerifyCheckpoints: cudaMalloc ple_conv");
  m.verify_ckpt_cap = num_ckpt;
  return Status();
}

Status ModelRestoreCheckpoint(const Model& m, int ckpt_idx,
                              cudaStream_t stream, int seq_id) {
  if (!m.d_verify_ssm_ckpt || ckpt_idx < 0 || ckpt_idx >= m.verify_ckpt_cap)
    return Status::Fail("ModelRestoreCheckpoint: invalid ckpt");
  const LinCkptDims d = CollectLinCkptDims(m);
  const int cap = m.verify_ckpt_cap;
  const size_t max_seq = static_cast<size_t>(m.cfg.max_seq);
  const size_t seq = static_cast<size_t>(seq_id);
  // Checkpoint source: pooled layout [num_layers, max_seq, cap, elems]; the
  // slice for (layer, sequence) starts at (layer_idx * max_seq + seq) * cap.
  // Restored into the same sequence's pooled recurrent-state slice. (The PLE
  // conv restore fixes the pre-existing single-seq gap: the PLE short-conv is
  // an in-place recurrence and must roll back on a partial accept too.)
  int lin_idx = 0;
  int ple_idx = 0;
  for (const auto& l : m.layers) {
    if (l.is_full_attention) continue;
    const size_t lin_slice =
        (static_cast<size_t>(lin_idx) * max_seq + seq) * cap;
    const float* ssm_src = m.d_verify_ssm_ckpt + lin_slice * d.ssm_elems +
                           static_cast<size_t>(ckpt_idx) * d.ssm_elems;
    const uint16_t* conv_src = m.d_verify_conv_ckpt + lin_slice * d.conv_elems +
                               static_cast<size_t>(ckpt_idx) * d.conv_elems;
    float* ssm_dst = l.ssm_state + seq * d.ssm_elems;
    uint16_t* conv_dst = l.conv_state + seq * d.conv_elems;
    if (cudaMemcpyAsync(ssm_dst, ssm_src, d.ssm_elems * sizeof(float),
                        cudaMemcpyDeviceToDevice, stream) != cudaSuccess)
      return Status::Fail("ModelRestoreCheckpoint: D2D ssm");
    if (cudaMemcpyAsync(conv_dst, conv_src, d.conv_elems * sizeof(uint16_t),
                        cudaMemcpyDeviceToDevice, stream) != cudaSuccess)
      return Status::Fail("ModelRestoreCheckpoint: D2D conv");
    lin_idx++;
    if (l.has_ple) {
      const size_t ple_slice =
          (static_cast<size_t>(ple_idx) * max_seq + seq) * cap;
      const uint16_t* ple_src =
          m.d_verify_ple_conv_ckpt + ple_slice * d.ple_conv_elems +
          static_cast<size_t>(ckpt_idx) * d.ple_conv_elems;
      uint16_t* ple_dst = l.ple_conv_state + seq * d.ple_conv_elems;
      if (cudaMemcpyAsync(ple_dst, ple_src, d.ple_conv_elems * sizeof(uint16_t),
                          cudaMemcpyDeviceToDevice, stream) != cudaSuccess)
        return Status::Fail("ModelRestoreCheckpoint: D2D ple_conv");
      ple_idx++;
    }
  }
  return Status();
}

// ---------------------------------------------------------------------------
// PD-ready 阶段边界 API (see model.h).
// ---------------------------------------------------------------------------

Status ModelBeginSequence(const Model& m, ModelSequence* seq,
                          cudaStream_t stream, int seq_id) {
  if (!seq) return Status::Fail("ModelBeginSequence: null seq");
  Status s = ResetAllLayers(m, stream, seq_id);
  if (!s.ok()) return s;
  seq->stage = ModelSequence::Stage::kPrefill;
  seq->position = 0;
  seq->seq_id = seq_id;
  seq->history.clear();
  return Status();
}

Status ModelPrefill(const Model& m, ModelSequence* seq,
                    const int32_t* input_ids, int T, uint16_t* logits,
                    cudaStream_t stream, uint16_t* trunk_out,
                    const VisionFeatures* vision, int seq_id) {
  if (!seq) return Status::Fail("ModelPrefill: null seq");
  if (seq->stage != ModelSequence::Stage::kPrefill) {
    return Status::Fail("ModelPrefill: sequence not in prefill stage");
  }
  const ModelConfig& cfg = m.cfg;
  if (T <= 0) return Status::Fail("ModelPrefill: T must be > 0");
  if (T > cfg.max_prefill) {
    return Status::Fail("ModelPrefill: T exceeds max_prefill");
  }
  // Per-layer state was reset by ModelBeginSequence; run the prefill.
  Status s = RunPrefill(m, input_ids, T, logits, stream, trunk_out, vision,
                        seq_id);
  if (!s.ok()) return s;
  // Handoff point: per-layer KV/SSM state is now ready for decode (or for
  // PD separation — the runner can take ownership of the state here).
  seq->stage = ModelSequence::Stage::kDecode;
  seq->position = T;
  seq->history.assign(input_ids, input_ids + T);
  return Status();
}

Status ModelDecodeStepSeq(const Model& m, ModelSequence* seq,
                          int32_t token_id, uint16_t* logits,
                          cudaStream_t stream, uint16_t* trunk_out,
                          int seq_id) {
  if (!seq) return Status::Fail("ModelDecodeStepSeq: null seq");
  if (seq->stage != ModelSequence::Stage::kDecode) {
    return Status::Fail("ModelDecodeStepSeq: sequence not in decode stage");
  }
  const ModelConfig& cfg = m.cfg;
  const int position = seq->position;
  if (position >= cfg.max_len) {
    return Status::Fail("ModelDecodeStepSeq: position exceeds max_len");
  }
  // history = tokens already seen (prompt + previously decoded); the PLE
  // n-gram context for this token is the last (ngram_size-1) of them.
  Status s = ModelDecodeStep(m, token_id, position, seq->history.data(),
                             logits, stream, trunk_out, seq_id);
  if (!s.ok()) return s;
  // Advance the state machine.
  seq->position = position + 1;
  seq->history.push_back(token_id);
  return Status();
}

void ModelEndSequence(ModelSequence* seq) {
  if (!seq) return;
  seq->stage = ModelSequence::Stage::kIdle;
  seq->position = 0;
  seq->history.clear();
}

// ---------------------------------------------------------------------------
// Recurrent-state snapshot (speculative-decoding rollback). See model.h.
//
// The paged full-attention caches (KV + indexer) are written at absolute
// positions and grow monotonically; a speculative re-run of the same positions
// overwrites the same pages, so they need no rollback. Only the recurrent
// state (linear SSM/conv, PLE short-conv) is updated in place and cannot be
// rewound by position, so it is snapshotted before verification and restored
// after a partial accept.
// ---------------------------------------------------------------------------

namespace {

struct RecurrentStateRef {
  void* dev;
  size_t bytes;
};

// Collect every recurrent-state buffer (device pointer + byte size) across all
// layers, in a stable order. The paged KV/indexer buffers are deliberately
// excluded (see the note above).
std::vector<RecurrentStateRef> CollectRecurrentState(const Model& m) {
  std::vector<RecurrentStateRef> refs;
  for (const auto& l : m.layers) {
    if (l.ssm_state) {
      refs.push_back({l.ssm_state,
                      static_cast<size_t>(l.linear.nv) * l.linear.kd *
                          l.linear.vd * 4});
      refs.push_back({l.conv_state,
                      static_cast<size_t>(l.linear.in_qkv()) *
                          (l.linear.conv_k - 1) * 2});
    }
    if (l.ple_conv_state) {
      refs.push_back({l.ple_conv_state,
                      static_cast<size_t>(l.hc_dim) *
                          (l.ple.conv_kernel - 1) * l.ple.conv_dilation * 2});
    }
  }
  return refs;
}

}  // namespace

void ModelStateSnapshot::Free() {
  data.clear();
  data.shrink_to_fit();
  bytes = 0;
  valid = false;
}

size_t ModelStateSnapshotBytes(const Model& m) {
  size_t total = 0;
  for (const auto& r : CollectRecurrentState(m)) total += r.bytes;
  return total;
}

Status ModelSnapshotState(const Model& m, ModelStateSnapshot* snap,
                          cudaStream_t stream) {
  if (!snap) return Status::Fail("ModelSnapshotState: null");
  const auto refs = CollectRecurrentState(m);
  size_t total = 0;
  for (const auto& r : refs) total += r.bytes;
  if (total == 0) {
    snap->Free();
    return Status();
  }
  snap->data.resize(total);
  size_t off = 0;
  for (const auto& r : refs) {
    if (cudaMemcpyAsync(snap->data.data() + off, r.dev, r.bytes,
                        cudaMemcpyDeviceToHost, stream) != cudaSuccess) {
      return Status::Fail("ModelSnapshotState: D2H copy");
    }
    off += r.bytes;
  }
  // Synchronize so the host buffer is fully populated before the caller uses
  // it (the snapshot is a host-side copy).
  if (cudaStreamSynchronize(stream) != cudaSuccess) {
    return Status::Fail("ModelSnapshotState: sync");
  }
  snap->bytes = total;
  snap->valid = true;
  return Status();
}

Status ModelRestoreState(const Model& m, const ModelStateSnapshot& snap,
                         cudaStream_t stream) {
  if (!snap.valid) return Status::Fail("ModelRestoreState: invalid snapshot");
  const auto refs = CollectRecurrentState(m);
  size_t off = 0;
  for (const auto& r : refs) {
    if (cudaMemcpyAsync(r.dev, snap.data.data() + off, r.bytes,
                        cudaMemcpyHostToDevice, stream) != cudaSuccess) {
      return Status::Fail("ModelRestoreState: H2D copy");
    }
    off += r.bytes;
  }
  // Synchronize so the device state is fully restored before the caller runs
  // the next forward (the re-run reads the recurrent state immediately).
  if (cudaStreamSynchronize(stream) != cudaSuccess) {
    return Status::Fail("ModelRestoreState: sync");
  }
  return Status();
}

}  // namespace model
}  // namespace q4t
