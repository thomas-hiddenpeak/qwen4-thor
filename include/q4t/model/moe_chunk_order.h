// Host-only selection among existing MoE token chunks. No token regrouping.
#pragma once

#include <algorithm>
#include <bit>
#include <cstddef>
#include <cstdint>
#include <cstring>
#include <vector>

namespace q4t::model {

// Strict opt-in. The caller reports invalid values instead of silently
// changing the requested experiment. nullptr is the default original order.
inline int ParseMoEChunkOrder(const char* value) {
  if (value == nullptr || std::strcmp(value, "0") == 0) return 0;
  if (std::strcmp(value, "1") == 0) return 1;
  return -1;
}

class MoEChunkOrderSelector {
 public:
  // Initialize only for enabled multi-chunk prefill. Expert IDs supplied to
  // AddExpert and by slot_expert must already have passed runtime validation.
  // Payload: chunks * ceil(experts/64) * 8 + chunks + ceil(experts/64) * 8
  // bytes; no device memory and no per-selection allocation.
  void Init(size_t chunks, int experts) {
    chunks_ = chunks;
    words_ = (static_cast<size_t>(experts) + 63) / 64;
    masks_.assign(chunks_ * words_, 0);
    selected_.assign(chunks_, 0);
    resident_.assign(words_, 0);
  }

  void AddExpert(size_t chunk, int expert) {
    masks_[chunk * words_ + static_cast<size_t>(expert) / 64] |=
        uint64_t{1} << (expert % 64);
  }

  // Read actual current slot identities after the prior chunk's host commits
  // have completed and immediately before PlanResolve. The max overlap tie
  // goes to the smallest original chunk index, matching offload_replay.py.
  // Return chunks_ if exhausted. One call costs O(slots + chunks * words_).
  template <typename SlotExpert>
  size_t SelectNext(int slots, SlotExpert slot_expert) {
    std::fill(resident_.begin(), resident_.end(), 0);
    for (int slot = 0; slot < slots; ++slot) {
      const int expert = slot_expert(slot);
      if (expert < 0) continue;
      resident_[static_cast<size_t>(expert) / 64] |=
          uint64_t{1} << (expert % 64);
    }
    size_t best = chunks_;
    int best_overlap = -1;
    for (size_t chunk = 0; chunk < chunks_; ++chunk) {
      if (selected_[chunk]) continue;
      int overlap = 0;
      for (size_t word = 0; word < words_; ++word) {
        overlap += std::popcount(masks_[chunk * words_ + word] &
                                 resident_[word]);
      }
      if (overlap > best_overlap) {
        best = chunk;
        best_overlap = overlap;
      }
    }
    if (best != chunks_) selected_[best] = 1;
    return best;
  }

 private:
  size_t chunks_ = 0;
  size_t words_ = 0;
  std::vector<uint64_t> masks_;
  std::vector<uint8_t> selected_;
  std::vector<uint64_t> resident_;
};

}  // namespace q4t::model
