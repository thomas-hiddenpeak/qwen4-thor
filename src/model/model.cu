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
  freep(d_emb);
  freep(d_trunk);
  freep(d_trunk2);
  freep(d_ple_emb);
  freep(d_img_pos);
  freep(d_ws);
  freep(d_verify_ssm_ckpt);
  freep(d_verify_conv_ckpt);
  d_ids = nullptr;
  d_positions = nullptr;
  d_emb = nullptr;
  d_trunk = nullptr;
  d_trunk2 = nullptr;
  d_ple_emb = nullptr;
  d_img_pos = nullptr;
  d_ws = nullptr;
  d_verify_ssm_ckpt = nullptr;
  d_verify_conv_ckpt = nullptr;
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
                         cfg.max_len, &m->layers[l], stream);
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
                 uint16_t* conv_ckpt = nullptr, int num_ckpt = 0) {
  const ModelConfig& cfg = m.cfg;
  const uint16_t* trunk = trunk_in;
  uint16_t* next = trunk2;
  int lin_idx = 0;  // running linear-layer index (for checkpoint offsets)
  for (int l = 0; l < cfg.num_layers; ++l) {
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
    float* layer_ssm_ckpt = nullptr;
    uint16_t* layer_conv_ckpt = nullptr;
    if (ssm_ckpt && !m.layers[l].is_full_attention) {
      const auto& lin = m.layers[l].linear;
      const size_t ssm_elems =
          static_cast<size_t>(lin.nv) * lin.kd * lin.vd;
      const size_t conv_elems =
          static_cast<size_t>(lin.in_qkv()) * (lin.conv_k - 1);
      layer_ssm_ckpt =
          ssm_ckpt + static_cast<size_t>(lin_idx) * num_ckpt * ssm_elems;
      layer_conv_ckpt =
          conv_ckpt + static_cast<size_t>(lin_idx) * num_ckpt * conv_elems;
    }
    Status s = DecoderLayerForward(m.layers[l], trunk, ple_emb, next,
                                   m.d_positions, T, m.d_ws, m.ws_bytes, stream,
                                   layer_ssm_ckpt, layer_conv_ckpt, num_ckpt);
    if (!s.ok()) return s;
    if (!m.layers[l].is_full_attention) lin_idx++;
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
  return HeadForward(m.head, trunk, logits, T, m.d_ws, m.ws_bytes, stream);
}

// Reset all per-layer persistent state (linear SSM/conv, full KV/indexer)
// to zero — prepare to process a fresh sequence from the start.
// const: ResetState only touches device memory, not the object.
Status ResetAllLayers(const Model& m, cudaStream_t stream) {
  for (const auto& l : m.layers) l.ResetState(stream);
  return Status();
}

// Prefill without reset: assumes per-layer state is already at the sequence
// start (call ResetAllLayers / ModelBeginSequence first). positions = 0..T-1.
// `trunk_out` (MTP scheme A hook) is forwarded to RunLayers. `vision` (optional)
// replaces image-token embeddings with vision features before ExpandTrunk.
Status RunPrefill(const Model& m, const int32_t* input_ids, int T,
                  uint16_t* logits, cudaStream_t stream,
                  uint16_t* trunk_out = nullptr,
                  const VisionFeatures* vision = nullptr) {
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
                   logits, stream, trunk_out);
}

Status ModelForward(const Model& m, const int32_t* input_ids, int T,
                    uint16_t* logits, cudaStream_t stream,
                    const VisionFeatures* vision) {
  const ModelConfig& cfg = m.cfg;
  if (T <= 0) return Status();
  if (T > cfg.max_prefill) {
    return Status::Fail("ModelForward: T exceeds max_prefill");
  }
  // Prefill of a fresh sequence starts from empty per-layer state (linear
  // SSM/conv, full KV/indexer). Reset so repeated calls are deterministic.
  Status s = ResetAllLayers(m, stream);
  if (!s.ok()) return s;
  return RunPrefill(m, input_ids, T, logits, stream, nullptr, vision);
}

Status ModelDecodeStep(const Model& m, int32_t token_id, int position,
                       const int32_t* history, uint16_t* logits,
                       cudaStream_t stream, uint16_t* trunk_out) {
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
                   logits, stream, trunk_out);
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
                        uint16_t* trunk_out, bool save_checkpoints) {
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
                   logits, stream, trunk_out, ssm_ckpt, conv_ckpt, num_ckpt);
}

// Per-linear-layer SSM/conv element counts (assumes all linear layers share
// the same dims, which they do for this architecture).
namespace {
struct LinCkptDims {
  size_t ssm_elems = 0;
  size_t conv_elems = 0;
  int num_lin = 0;
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
  m.d_verify_ssm_ckpt = nullptr;
  m.d_verify_conv_ckpt = nullptr;
  const size_t ssm_bytes = static_cast<size_t>(d.num_lin) * num_ckpt *
                           d.ssm_elems * sizeof(float);
  const size_t conv_bytes = static_cast<size_t>(d.num_lin) * num_ckpt *
                            d.conv_elems * sizeof(uint16_t);
  if (cudaMalloc(reinterpret_cast<void**>(&m.d_verify_ssm_ckpt), ssm_bytes) !=
      cudaSuccess)
    return Status::Fail("ModelReserveVerifyCheckpoints: cudaMalloc ssm");
  if (cudaMalloc(reinterpret_cast<void**>(&m.d_verify_conv_ckpt), conv_bytes) !=
      cudaSuccess)
    return Status::Fail("ModelReserveVerifyCheckpoints: cudaMalloc conv");
  m.verify_ckpt_cap = num_ckpt;
  return Status();
}

Status ModelRestoreCheckpoint(const Model& m, int ckpt_idx,
                              cudaStream_t stream) {
  if (!m.d_verify_ssm_ckpt || ckpt_idx < 0 || ckpt_idx >= m.verify_ckpt_cap)
    return Status::Fail("ModelRestoreCheckpoint: invalid ckpt");
  const LinCkptDims d = CollectLinCkptDims(m);
  const int cap = m.verify_ckpt_cap;
  int lin_idx = 0;
  for (const auto& l : m.layers) {
    if (l.is_full_attention) continue;
    const float* ssm_src = m.d_verify_ssm_ckpt +
                           (static_cast<size_t>(lin_idx) * cap + ckpt_idx) *
                               d.ssm_elems;
    const uint16_t* conv_src = m.d_verify_conv_ckpt +
                               (static_cast<size_t>(lin_idx) * cap + ckpt_idx) *
                                   d.conv_elems;
    if (cudaMemcpyAsync(l.ssm_state, ssm_src, d.ssm_elems * sizeof(float),
                        cudaMemcpyDeviceToDevice, stream) != cudaSuccess)
      return Status::Fail("ModelRestoreCheckpoint: D2D ssm");
    if (cudaMemcpyAsync(l.conv_state, conv_src,
                        d.conv_elems * sizeof(uint16_t),
                        cudaMemcpyDeviceToDevice, stream) != cudaSuccess)
      return Status::Fail("ModelRestoreCheckpoint: D2D conv");
    lin_idx++;
  }
  return Status();
}

// ---------------------------------------------------------------------------
// PD-ready 阶段边界 API (see model.h).
// ---------------------------------------------------------------------------

Status ModelBeginSequence(const Model& m, ModelSequence* seq,
                          cudaStream_t stream) {
  if (!seq) return Status::Fail("ModelBeginSequence: null seq");
  Status s = ResetAllLayers(m, stream);
  if (!s.ok()) return s;
  seq->stage = ModelSequence::Stage::kPrefill;
  seq->position = 0;
  seq->history.clear();
  return Status();
}

Status ModelPrefill(const Model& m, ModelSequence* seq,
                    const int32_t* input_ids, int T, uint16_t* logits,
                    cudaStream_t stream, uint16_t* trunk_out,
                    const VisionFeatures* vision) {
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
  Status s = RunPrefill(m, input_ids, T, logits, stream, trunk_out, vision);
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
                          cudaStream_t stream, uint16_t* trunk_out) {
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
                             logits, stream, trunk_out);
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
