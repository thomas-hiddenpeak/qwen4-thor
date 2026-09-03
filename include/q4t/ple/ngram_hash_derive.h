// PLE n-gram hash: splitmix64 derivation of the layer multipliers.
//
// The checkpoint stores the multipliers directly, so the runtime loads them.
// This header exists so we can *derive* the same values from the config seed
// and cross-check the loaded tensor (and, if a checkpoint ever lacks the
// tensor, regenerate it). It mirrors SGLang's
// `Qwen4ExpNGramEmbedding._build_layer_multipliers`.
#pragma once

#include <cstdint>
#include <vector>

namespace q4t {
namespace ple {

// splitmix64 finalizer.
inline uint64_t SplitMix64(uint64_t x) {
  x += 0x9E3779B97F4A7C15ULL;
  x = ((x ^ (x >> 30)) * 0xBF58476D1CE4E5B9ULL);
  x = ((x ^ (x >> 27)) * 0x94D049BB133111EBULL);
  return x ^ (x >> 31);
}

// Derive the `ngram_size` odd multipliers for a PLE layer.
//
// `base_seed = seed + 10007 * ple_layer_index`. For each position idx in
// [0, ngram_size): x0 = base_seed + GAMMA * (idx + 1); the multiplier is
// 2 * (splitmix64(x0) % half_bound) + 1, where half_bound =
// ((2^63 - 1) / max(unigram_vocab_size, 1)) / 2.
std::vector<int64_t> DeriveLayerMultipliers(int64_t seed,
                                            int ple_layer_index,
                                            int ngram_size,
                                            int unigram_vocab_size);

}  // namespace ple
}  // namespace q4t
