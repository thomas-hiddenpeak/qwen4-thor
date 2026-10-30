#pragma once

#include <algorithm>
#include <cstddef>
#include <span>

namespace q4t::runtime {

// Shared by ModelConfig and pre-load server option validation.
inline constexpr int kDefaultMaxPrefill = 2048;

// An empty list preserves the legacy uniform layout. Explicit capacities
// are whole KV pages; the list is immutable for the model's lifetime.
inline bool ValidSequenceCapacities(std::span<const int> capacities,
                                    int max_len, int max_seq) {
  if (capacities.empty()) return true;
  return max_seq >= 1 && max_seq <= 64 &&
         capacities.size() == static_cast<size_t>(max_seq) && max_len > 0 &&
         max_len <= 262144 &&
         *std::max_element(capacities.begin(), capacities.end()) == max_len &&
         std::all_of(capacities.begin(), capacities.end(), [max_len](int n) {
           // Fixed QSA compress=4: keep the short slot large enough for
           // a padded score window without reducing the long-slot top-k.
           return n >= std::min(2048, (max_len + 3) / 4) && n >= 16 &&
                  n <= max_len && n % 16 == 0;
         });
}

inline int SequenceCapacity(std::span<const int> capacities, int seq,
                            int uniform) {
  if (capacities.empty()) return uniform;
  return seq >= 0 && static_cast<size_t>(seq) < capacities.size()
             ? capacities[seq]
             : 0;
}

inline size_t SequenceOffset(std::span<const int> capacities, int seq,
                             int uniform) {
  if (capacities.empty()) return static_cast<size_t>(seq) * uniform;
  size_t offset = 0;
  for (int i = 0; i < seq; ++i) offset += capacities[i];
  return offset;
}

inline size_t SequencePoolTokens(std::span<const int> capacities, int max_len,
                                 int max_seq) {
  return SequenceOffset(capacities, max_seq, max_len);
}

// Choose the smallest currently free slot that can hold the entire request.
// Equal capacities use the lower slot ID, independently of request history.
template <typename FreeSlots>
int SelectSequenceSlot(std::span<const int> capacities, int max_len,
                       const FreeSlots& free, size_t required) {
  int selected = -1;
  for (size_t i = 0; i < free.size(); ++i) {
    const int capacity = SequenceCapacity(capacities, i, max_len);
    if (free[i] && required <= static_cast<size_t>(capacity) &&
        (selected < 0 ||
         capacity < SequenceCapacity(capacities, selected, max_len)))
      selected = static_cast<int>(i);
  }
  return selected;
}
}  // namespace q4t::runtime
