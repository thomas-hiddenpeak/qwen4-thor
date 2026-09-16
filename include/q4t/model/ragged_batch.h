// Ragged multi-sequence packing descriptor for a batched-prefill (or
// interleaved prefill/decode) forward. Shared by the linear-attention, PLE and
// decoder-layer forwards, so it lives in its own header to avoid an include
// cycle (decoder_layer.h includes linear_attention.h / ple_layer.h).
#pragma once

namespace q4t {
namespace model {

// When a `const RaggedBatch*` is non-null, the packed [T, ...] rows hold B
// variable-length sequences: sequence b occupies rows
// [seq_offset[b], seq_offset[b+1]) and token t sits at local position
// token_local[t] within its own sequence. This generalizes the uniform
// `tokens_per_seq` MTP-verify layout (offset[b] = b*Tps, local[t] = t % Tps) so
// the per-sequence causal linear/PLE kernels handle unequal prompt lengths in
// one packed forward. Null = legacy uniform path (bit-identical to the MTP
// sequence-major layout). `d_seq_id` still selects the pooled recurrent-state
// slice per packed token, exactly as in the uniform path.
struct RaggedBatch {
  const int* seq_offset = nullptr;   // device [B+1] cu_seqlens (offset[0]=0,
                                     // offset[B]=T)
  const int* token_local = nullptr;  // device [T] local position per token
  int B = 0;                         // number of packed sequences
};

}  // namespace model
}  // namespace q4t
