// Host metadata policy for the opt-in, explicit single-decode mirror rule.
#pragma once

#include <cstdint>
#include <cstring>
#include <span>
#include <vector>

namespace q4t::quant {

inline int ParseMoEMirrorGpuRecycle(const char* value) {
  if (!value || std::strcmp(value, "0") == 0) return 0;
  if (std::strcmp(value, "1") == 0) return 1;
  return -1;
}

inline bool MoEMirrorGpuRecycleScope(bool explicit_single_decode, int tokens,
                                     bool has_prefill_request) {
  return explicit_single_decode && tokens == 1 && !has_prefill_request;
}

// Call after the complete plan chose every victim, before dispatching workers.
// Only untouched resident slots cover a mirror throughout this plan. Incoming
// experts and all planned victims are excluded, independently of commit order.
// Preconditions: experts > 0 and reserved.size() == slots.size().
inline std::vector<uint8_t> BuildMoEMirrorGpuCoverage(
    int experts, std::span<const int> slots,
    std::span<const uint8_t> reserved) {
  std::vector<uint8_t> covered(experts, 0);
  for (size_t slot = 0; slot < slots.size(); ++slot) {
    const int expert = slots[slot];
    if (!reserved[slot] && expert >= 0 && expert < experts)
      covered[expert] = 1;
  }
  return covered;
}

struct MoEMirrorRecycleChoice {
  int slot = -1;
  int fallback = -1;
  bool preferred = false;
  bool Changed() const { return preferred && slot != fallback; }
};

// The caller holds the existing ring mutex. eligible() must retain the CUDA
// event/claim rules; this helper never inspects mutable GPU identity maps.
// Stop on the first eligible covered slot; otherwise use the original cursor
// fallback. Empty slots are not a second preference rule.
template <typename IsEligible>
MoEMirrorRecycleChoice PickMoEMirrorGpuRecycle(
    int cursor, std::span<const int> ring,
    std::span<const uint8_t> covered, IsEligible&& eligible) {
  MoEMirrorRecycleChoice choice;
  const int count = static_cast<int>(ring.size());
  for (int offset = 0; offset < count; ++offset) {
    const int candidate = (cursor + offset) % count;
    if (!eligible(candidate)) continue;
    if (choice.fallback < 0) choice.fallback = candidate;
    const int expert = ring[candidate];
    if (expert >= 0 && static_cast<size_t>(expert) < covered.size() &&
        covered[expert]) {
      choice.slot = candidate;
      choice.preferred = true;
      return choice;
    }
  }
  choice.slot = choice.fallback;
  return choice;
}

}  // namespace q4t::quant
