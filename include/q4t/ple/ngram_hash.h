// PLE n-gram hash: compute the 16 lookup row IDs for one token position.
//
// The PLE table is a giant n-gram hash lookup. For each token position we take
// a context window of `ngram_size` tokens (the `ngram_size - 1` preceding
// tokens plus the current token). For each n-gram order (2-gram, 3-gram) and
// each of `heads_per_ngram` heads, we hash the window with a per-layer odd
// multiplier (XOR mix) and reduce modulo a per-head prime vocabulary size,
// then add the head offset. The result is the row index into the PLE table.
//
// The multipliers / vocab sizes / offsets are loaded from the checkpoint
// (they are stored as tensors); this module only performs the hash. The
// splitmix64 derivation of the multipliers lives in ngram_hash_derive.h for
// development-time cross-checking only.
#pragma once

#include <cstdint>
#include <vector>

namespace q4t {
namespace ple {

// Parameters of the PLE n-gram hash for one PLE layer. All arrays are sized
// `ngram_heads = (ngram_size - 1) * heads_per_ngram`.
struct NgramHashParams {
  int ngram_size = 3;
  int heads_per_ngram = 8;
  int ngram_heads() const { return (ngram_size - 1) * heads_per_ngram; }

  // One odd multiplier per n-gram position (size = ngram_size). Loaded from
  // the checkpoint tensor `layer_multipliers`.
  std::vector<int64_t> multipliers;
  // Per-head prime vocabulary size (size = ngram_heads). Loaded from
  // `ngram_heads_vocab_sizes`.
  std::vector<int64_t> head_vocab_sizes;
  // Per-head row offset into the table (size = ngram_heads). Loaded from
  // `ngram_heads_offsets`.
  std::vector<int64_t> head_offsets;
};

// Compute the `ngram_heads` row IDs for a single token position.
//
// `context` holds the `ngram_size`-token window ordered oldest -> newest,
// i.e. context[0] = t_{-(ngram_size-1)}, ..., context[ngram_size-1] = t_0
// (the current token). `eos_token_id` drives the EOS-ignoring shift rule
// (matching SGLang's `_shift_right_ignore_eos`): the token `k` steps before
// the current one contributes its real value only if there is no EOS strictly
// between it and the current token; otherwise it is replaced by
// `eos_token_id`.
//
// Output has `params.ngram_heads()` entries: heads for the 2-gram order come
// first, then the 3-gram order, matching the SGLang reference layout.
void ComputeNgramRowIds(const NgramHashParams& params,
                        const int64_t* context,
                        int64_t eos_token_id,
                        int64_t* out_row_ids);

}  // namespace ple
}  // namespace q4t
