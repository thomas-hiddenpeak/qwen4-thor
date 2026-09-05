// Model head/tail — embedding lookup, trunk expansion, and the closing
// hyper-connection mixer + lm_head.
//
// The qwen4_exp model forward is:
//   1. emb   = embed_tokens(input_ids)            [T, hs]
//   2. trunk = replicate(emb, hc)                 [T, hc*hs]  (hc identical branches)
//   3. for each of 48 layers: trunk = DecoderLayerForward(trunk, ...)
//   4. mixed = hyper_connection_mixer.mix(trunk)  [T, hs]     (use_combine=false)
//   5. logits = lm_head(mixed)                    [T, vocab]
//
// This module provides steps 1, 2, 4, and 5 (step 3 is the decoder-layer loop,
// see decoder_layer.h). The mixer is a GatedResidual with use_combine=false
// (no block_inject); it mixes the hc branches back to a single [T, hs] vector.
// lm_head is a plain BF16 GEMM (vocab x hs).
#pragma once

#include <cuda_runtime.h>

#include <cstdint>
#include <string>

#include "q4t/io/weight_loader.h"
#include "q4t/model/hyperconnection.h"
#include "q4t/status.h"

namespace q4t {
namespace model {

// Device weights of the model head/tail. All BF16 (uint16), row-major.
struct ModelHeadWeights {
  int vocab = 248320;
  int hs = 2560;
  int hc = 4;
  int hc_dim = 10240;

  uint16_t* embed_tokens = nullptr;  // [vocab, hs]
  uint16_t* lm_head = nullptr;  // [vocab, hs]
  HyperConnectionWeights mixer;  // use_combine=false (no block_inject)

  void Free();
};

// Load the model head/tail weights from `loader`:
//   model.language_model.embed_tokens.weight   [vocab, hs]
//   lm_head.weight                             [vocab, hs]
//   model.language_model.hyper_connection_mixer.{hc_norm,
//     input_mix_weight_down, input_mix_weight_up}.weight   (GatedResidual)
Status LoadModelHead(const io::WeightLoader& loader, int vocab, int hs, int hc,
                     int lowrank, float eps, ModelHeadWeights* out,
                     cudaStream_t stream);

// token_ids [T] (device int32) -> emb [T, hs] BF16 (row gather).
Status EmbedLookup(const ModelHeadWeights& w, const int32_t* token_ids,
                   uint16_t* emb, int T, cudaStream_t stream);

// emb [T, hs] -> trunk [T, hc*hs] BF16; each of the hc branches is a copy of
// emb (the trunk residual starts as hc identical branches).
Status ExpandTrunk(const ModelHeadWeights& w, const uint16_t* emb,
                   uint16_t* trunk, int T, cudaStream_t stream);

// trunk [T, hc*hs] -> logits [T, vocab] BF16: mixer.mix(trunk) -> [T, hs],
// then lm_head GEMM.
Status HeadForward(const ModelHeadWeights& w, const uint16_t* trunk,
                   uint16_t* logits, int T, void* workspace,
                   size_t workspace_bytes, cudaStream_t stream);

// Device bytes for the HeadForward `workspace` (mixer GEMM + lm_head GEMM).
size_t ModelHeadWorkspaceBytes(int T, int hs);

}  // namespace model
}  // namespace q4t
