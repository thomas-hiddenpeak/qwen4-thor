// Allocation envelope for optional models sharing one execution owner.
#pragma once

#include <algorithm>
#include <cstdint>
#include <limits>

#include "q4t/status.h"

namespace q4t::server {
struct AuxiliaryResourceBudget {
  uint64_t resident_bytes = 0;
  uint64_t pending_host_bytes = 0;
  uint64_t sequence_bytes = 0;

  // Both groups must be fully included in resident_bytes first. Only their
  // device weights are mutually exclusive; metadata/worker margins persist.
  Status ShareWeights(uint64_t first, uint64_t second) {
    if (first > resident_bytes || second > resident_bytes - first)
      return Status::Fail("auxiliary resident groups exceed charged weights");
    resident_bytes -= std::min(first, second);
    return {};
  }

  // All weights/metadata and pending HTTP bodies remain charged. The caller
  // must serialize loads and workspaces under one owned sequence slot before
  // taking the maximum of their transient allocations. Metadata-only reads
  // stay within the separate per-model resident host margins.
  Status Add(uint64_t resident, uint64_t pending, uint64_t loading,
             uint64_t executing) {
    constexpr auto maximum = std::numeric_limits<uint64_t>::max();
    if (resident > maximum - resident_bytes ||
        pending > maximum - pending_host_bytes)
      return Status::Fail("auxiliary allocation estimate overflow");
    const auto next_resident = resident_bytes + resident;
    const auto next_pending = pending_host_bytes + pending;
    const auto next_sequence = std::max({sequence_bytes, loading, executing});
    if (next_pending > maximum - next_resident ||
        next_sequence > maximum - next_resident - next_pending)
      return Status::Fail("auxiliary allocation estimate overflow");
    resident_bytes = next_resident;
    pending_host_bytes = next_pending;
    sequence_bytes = next_sequence;
    return {};
  }
};
}  // namespace q4t::server
