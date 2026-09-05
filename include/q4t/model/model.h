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
  int* d_positions = nullptr;  // [max_prefill]
  uint16_t* d_emb = nullptr;  // [max_prefill, hs]
  uint16_t* d_trunk = nullptr;  // [max_prefill, hc*hs] (ping)
  uint16_t* d_trunk2 = nullptr;  // [max_prefill, hc*hs] (pong)
  uint16_t* d_ple_emb = nullptr;  // [max_prefill, ple_embed_dim]
  void* d_ws = nullptr;  // forward workspace (reused across layers)
  size_t ws_bytes = 0;

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

// Run one prefill forward: input_ids [T] (host int32) -> logits [T, vocab]
// (device BF16). T must be <= cfg.max_prefill. Prefill starts from empty
// per-layer state (positions 0..T-1).
Status ModelForward(const Model& m, const int32_t* input_ids, int T,
                    uint16_t* logits, cudaStream_t stream);

// Run one decode step for a single new token: token_id at absolute position
// `position` -> logits [1, vocab] (device BF16). Per-layer state is NOT reset
// (it continues from the prior steps, so call ModelForward first for the
// prompt). `history` is the host int32 array of the tokens already generated
// before this one (prompt + previously decoded tokens), length >= position;
// it supplies the PLE n-gram context for the new token.
Status ModelDecodeStep(const Model& m, int32_t token_id, int position,
                       const int32_t* history, uint16_t* logits,
                       cudaStream_t stream);

}  // namespace model
}  // namespace q4t
