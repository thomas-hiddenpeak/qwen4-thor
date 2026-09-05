// Complete qwen4_exp decoder layer — wires the verified building blocks into
// one layer's forward:
//
//   hyper_input [T, hc*hs]
//     1. [PLE, layer 1 only]  hyper_input += ple(ple_embeddings, hyper_input)
//     2. mixed_attn, res_a = attn_hc.mix(hyper_input)         [T, hs]
//     3. attn_out = attn_block(mixed_attn)                    linear or full
//     4. combined_a = attn_hc.combine(attn_out, res_a)        [T, hc*hs]
//     5. mixed_mlp, res_m = mlp_hc.mix(combined_a)            [T, hs]
//     6. mlp_out = moe(mixed_mlp)                             [T, hs]
//     7. out = mlp_hc.combine(mlp_out, res_m)                 [T, hc*hs]
//
// The layer owns all sub-block weights (attn + MoE + the two Hyper-Connection
// GatedResiduals + optional PLE) and the per-layer persistent caches (linear
// SSM/conv state, full-attention KV + indexer caches). It allocates one device
// `workspace` (see DecoderLayerWorkspaceBytes) and carves it into per-submodule
// regions.
//
// PLE injection (step 1) is wired for layer 1 (checkpoint `ple_layer_ids = [2]`
// is 1-indexed). The caller passes the gathered n-gram embeddings
// (`ple_embeddings`, [T, ple_embed_dim] BF16) to DecoderLayerForward; when
// non-null and `layer.has_ple`, the PLE correction is added to hyper_input
// before the attention HC mix.
#pragma once

#include <cuda_runtime.h>

#include <cstdint>
#include <string>

#include "q4t/io/weight_loader.h"
#include "q4t/model/full_attention.h"
#include "q4t/model/hyperconnection.h"
#include "q4t/model/linear_attention.h"
#include "q4t/model/moe.h"
#include "q4t/model/ple_layer.h"
#include "q4t/quant/moe_weights.h"
#include "q4t/status.h"

namespace q4t {
namespace model {

// One qwen4_exp decoder layer (all 48 layers share this structure; the
// attention block is linear_attention for 36 layers and full_attention for 12).
struct DecoderLayer {
  int layer_id = 0;
  int hs = 2560;
  int hc = 4;
  int hc_dim = 10240;
  int topk = 10;  // num_experts_per_tok (MoE router top-k)
  bool is_full_attention = false;  // layer_id % 4 == 3
  bool has_ple = false;  // layer_id + 1 in ple_layer_ids (0-indexed layer 1)

  // Attention block (one of the two, the other's weights stay null).
  LinearAttentionWeights linear;
  FullAttentionWeights full;

  // PLE layer (only loaded for the PLE layer; weights stay null otherwise).
  PleLayerWeights ple;

  // MoE (every layer).
  quant::MoEWeightLayout routed;
  MoEExtraWeights mlp;

  // Hyper-Connection GatedResiduals (both use_mix + use_combine).
  HyperConnectionWeights attn_hc;
  HyperConnectionWeights mlp_hc;

  // Per-layer persistent caches (allocated in Load, freed in Free).
  uint16_t* ssm_state = nullptr;  // linear: [nv, kd, vd]
  uint16_t* conv_state = nullptr;  // linear: [in_qkv, conv_k-1]
  uint16_t* kv_cache = nullptr;  // full: [max_len, nkv, 2, hd]
  uint16_t* idx_raw = nullptr;  // full: [max_len, idx_hd]
  uint16_t* idx_comp = nullptr;  // full: [max_len, idx_hd]

  // Max sequence length the full-attention caches are sized for.
  int max_len = 2048;

  void Free();
  // Reset the per-layer persistent state (linear SSM/conv, or full KV/indexer)
  // to zero — i.e. prepare to process a fresh sequence from the start.
  // const: only touches device memory, not the object's ownership.
  void ResetState(cudaStream_t stream) const;
};

// Device bytes required for the DecoderLayerForward `workspace` argument for a
// layer of type `is_full_attention` handling up to `T` tokens. `has_ple` adds
// the PLE layer's GEMM scratch + carved intermediates.
size_t DecoderLayerWorkspaceBytes(int T, bool is_full_attention, bool has_ple,
                                  int hs, int E, int moe_is, int shared_is,
                                  int k);

// Load one decoder layer's weights from `loader`.
//
//   layer_id : 0-based layer index (determines the attention block type and
//              the checkpoint prefixes)
//   E        : num_experts (512)
//   moe_is   : moe_intermediate_size (640)
//   shared_is: shared_expert_intermediate_size (640)
//   k        : num_experts_per_tok (10)
//   max_len  : full-attention cache length (linear layers ignore it)
//
// Checkpoint prefixes (base = "model.language_model.layers.{layer_id}"):
//   {base}.attn_hyper_connection / {base}.mlp_hyper_connection  (HC)
//   {base}.linear_attn  (linear layers)  or  {base}.self_attn   (full layers)
//   {base}.mlp  (MoE routed + router/shared)
Status LoadDecoderLayer(const io::WeightLoader& loader, int layer_id, int hs,
                        int hc, int lowrank, float eps, int E, int moe_is,
                        int shared_is, int k, int max_len, DecoderLayer* out,
                        cudaStream_t stream);

// Run one decoder layer forward for a single sequence (prefill).
//
//   hyper_input    : device [T, hc*hs] uint16 (BF16) — the trunk residual
//   ple_embeddings : device [T, ple_embed_dim] uint16 (BF16) gathered n-gram
//                    embeddings; must be non-null when layer.has_ple
//   out            : device [T, hc*hs] uint16 (BF16) — updated trunk residual
//   positions      : host int [T] absolute positions (full attention only)
//   T              : number of tokens
//   workspace      : device scratch (>= DecoderLayerWorkspaceBytes)
Status DecoderLayerForward(const DecoderLayer& layer, const uint16_t* hyper_input,
                           const uint16_t* ple_embeddings, uint16_t* out,
                           const int* positions, int T, void* workspace,
                           size_t workspace_bytes, cudaStream_t stream);

}  // namespace model
}  // namespace q4t
