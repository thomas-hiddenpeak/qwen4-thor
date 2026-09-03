// PLE n-gram hash implementation.
#include "q4t/ple/ngram_hash.h"

#include <cstddef>

namespace q4t {
namespace ple {

void ComputeNgramRowIds(const NgramHashParams& params,
                        const int64_t* context,
                        int64_t eos_token_id,
                        int64_t* out_row_ids) {
  const int size = params.ngram_size;
  const int hpn = params.heads_per_ngram;
  const int heads = params.ngram_heads();
  if (static_cast<int>(params.multipliers.size()) < size ||
      static_cast<int>(params.head_vocab_sizes.size()) < heads ||
      static_cast<int>(params.head_offsets.size()) < heads) {
    // Malformed params; leave output untouched. Callers validate at load time.
    return;
  }

  // For each shift `k` (0 = current token, k = k steps back), determine the
  // effective token under the EOS-ignoring rule: the token k steps back is
  // context[size-1-k]; it is valid only if no EOS lies strictly between it and
  // the current token, i.e. in context[size-k .. size-2]. Otherwise it is EOS.
  // (Matches SGLang's _shift_right_ignore_eos at the current position.)
  int64_t eff[size];
  for (int k = 0; k < size; ++k) {
    const int idx = size - 1 - k;
    int64_t tok = context[idx];
    if (k > 0) {
      for (int j = size - k; j < size - 1; ++j) {
        if (context[j] == eos_token_id) {
          tok = eos_token_id;
          break;
        }
      }
    }
    eff[k] = tok;
  }

  // For an n-gram of order `n` (n = 2..size), the mix at the current position
  // is (matching SGLang's _hash_contexts, where shifted_tokens[0] is the
  // unshifted window so its last element is the current token):
  //   mix = eff[0]*m[0] XOR eff[1]*m[1] XOR ... XOR eff[n-1]*m[n-1]
  for (int ngram = 2; ngram <= size; ++ngram) {
    const int ngram_idx = ngram - 2;
    const int start = ngram_idx * hpn;
    uint64_t mix = 0;
    for (int pos = 0; pos < ngram; ++pos) {
      mix ^= static_cast<uint64_t>(eff[pos]) *
             static_cast<uint64_t>(params.multipliers[pos]);
    }
    for (int h = 0; h < hpn; ++h) {
      const int head = start + h;
      const int64_t vocab = params.head_vocab_sizes[head];
      const int64_t offset = params.head_offsets[head];
      const int64_t rem =
          static_cast<int64_t>(mix % static_cast<uint64_t>(vocab));
      out_row_ids[head] = rem + offset;
    }
  }
}

}  // namespace ple
}  // namespace q4t
