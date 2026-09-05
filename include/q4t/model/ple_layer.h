// PLE layer (Per-Layer Embedding) forward — the qwen4_exp n-gram SSD-stream
// injection.
//
// PLE sits as a sibling of the attention block in ONE decoder layer (the
// checkpoint's `ple_layer_ids = [2]` is 1-indexed, i.e. 0-indexed layer 1).
// It takes the gathered n-gram embeddings (from the 51.2 GB FP8 SSD table,
// already reduced to [T, ple_embed_dim] BF16 by the embedding module) plus the
// layer's hyper-connection input, and produces a [T, hc*hs] correction that is
// ADDED to the hyper-connection input BEFORE the attention HC mix.
//
// Math (mirrors SGLang Qwen4ExpPLELayer.forward, prefill path):
//   key   = key_proj(embeddings)        [T, hc*hs]   (BF16 GEMM)
//   value = value_proj(embeddings)      [T, hs]      (BF16 GEMM)
//   key_n   = GroupedGemmaRMSNorm(key,   norm_key)    per-branch (group = hs)
//   query_n = GroupedGemmaRMSNorm(hyper_input, norm_query)
//   gate[b] = sqrt(|sum_c key_n[b,c]*query_n[b,c]| / sqrt(hs))
//   gate    = sigmoid(gate);  gated_value[b,c] = gate[b] * value[c]   [T, hc*hs]
//   gated_n = GroupedGemmaRMSNorm(gated_value, norm_conv)
//   conv_out = silu( depthwise-causal-conv(gated_n) )   kernel=4, dilation=3
//   out = gated_value + conv_out                              [T, hc*hs]
//
// The conv is a depthwise causal convolution over the token sequence:
//   conv_out[t, c] = silu( sum_{j=0}^{K-1} conv1d[c, j] *
//                          gated_n[t - (K-1-j)*dilation, c] )
// with zero padding before the sequence start. (The decode fast path keeps a
// persistent conv_state; the prefill path used here is equivalent for a fresh
// sequence and is what the first forward sees.)
//
// GroupedGemmaRMSNorm is the SAME op as the hyper-connection norm: split the
// hc*hs vector into hc groups of hs, RMSNorm each group, scale by (1 + w).
#pragma once

#include <cuda_runtime.h>

#include <cstdint>
#include <string>

#include "q4t/io/weight_loader.h"
#include "q4t/status.h"

namespace q4t {
namespace model {

// Device weights of one PLE layer. All BF16 (uint16), row-major.
struct PleLayerWeights {
  int hc_count = 4;
  int hidden_size = 2560;
  int ple_embed_dim = 2560;
  int conv_kernel = 4;
  int conv_dilation = 3;  // = ngram_size
  float eps = 1e-6f;

  uint16_t* key_proj = nullptr;   // [hc*hs, ple_embed_dim]
  uint16_t* value_proj = nullptr;  // [hs, ple_embed_dim]
  uint16_t* norm_key = nullptr;  // [hc*hs]
  uint16_t* norm_query = nullptr;  // [hc*hs]
  uint16_t* norm_conv = nullptr;  // [hc*hs]
  uint16_t* conv1d = nullptr;  // [hc*hs, conv_kernel]

  int hc_dim() const { return hc_count * hidden_size; }
  void Free();
};

// Load one PLE layer's weights from `loader` under the checkpoint names
//   {prefix}.key_proj.weight
//   {prefix}.value_proj.weight
//   {prefix}.norm_key.weight
//   {prefix}.norm_query.weight
//   {prefix}.norm_conv.weight
//   {prefix}.conv1d.weight
// e.g. prefix = "model.language_model.layers.1.ple".
Status LoadPleLayer(const io::WeightLoader& loader, const std::string& prefix,
                    int hc_count, int hidden_size, int ple_embed_dim,
                    int conv_kernel, int conv_dilation, float eps,
                    PleLayerWeights* out, cudaStream_t stream);

// Run the PLE layer forward.
//
//   embeddings  : device row-major [T, ple_embed_dim] uint16 (BF16), the
//                 gathered + reduced n-gram embeddings
//   hyper_input : device row-major [T, hc*hs] uint16 (BF16), the layer's
//                 hyper-connection input (the "query")
//   out         : device row-major [T, hc*hs] uint16 (out) — ADD to
//                 hyper_input to form the PLE-corrected input
//   workspace   : scratch device buffer (>= ~32 MiB) for the two GEMMs
Status PleLayerForward(const PleLayerWeights& w, const uint16_t* embeddings,
                       const uint16_t* hyper_input, uint16_t* out, int T,
                       void* workspace, size_t workspace_bytes,
                       cudaStream_t stream);

}  // namespace model
}  // namespace q4t
