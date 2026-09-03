// Derivation of PLE layer multipliers (development cross-check).
#include "q4t/ple/ngram_hash_derive.h"

#include <cstdint>
#include <limits>

namespace q4t {
namespace ple {

namespace {
constexpr uint64_t kGamma = 0x9E3779B97F4A7C15ULL;
constexpr int64_t kPrime1 = 10007;
}  // namespace

std::vector<int64_t> DeriveLayerMultipliers(int64_t seed,
                                            int ple_layer_index,
                                            int ngram_size,
                                            int unigram_vocab_size) {
  const int64_t max_long = std::numeric_limits<int64_t>::max();
  const int64_t vocab = unigram_vocab_size > 0 ? unigram_vocab_size : 1;
  const int64_t m_max = max_long / vocab;
  const int64_t half_bound = m_max / 2 > 0 ? m_max / 2 : 1;

  const uint64_t base_seed =
      static_cast<uint64_t>(seed + kPrime1 * ple_layer_index);

  std::vector<int64_t> values;
  values.reserve(ngram_size);
  for (int idx = 0; idx < ngram_size; ++idx) {
    const uint64_t x0 = base_seed + kGamma * static_cast<uint64_t>(idx + 1);
    const uint64_t mixed = SplitMix64(x0);
    values.push_back(2 * static_cast<int64_t>(mixed % static_cast<uint64_t>(half_bound)) + 1);
  }
  return values;
}

}  // namespace ple
}  // namespace q4t
