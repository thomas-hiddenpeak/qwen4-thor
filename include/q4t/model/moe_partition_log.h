// Logging policy only; never selects MoE partitions or changes computation.
#pragma once

#include <cstring>

namespace q4t::model {

// Unknown includes prefill, continuation, and callers without a phase contract.
enum class MoEPartitionLogPhase { kUnknown, kSingleDecode };

inline int ParseMoEDecodePartitionLogQuietMode(const char* value) {
  if (value == nullptr || std::strcmp(value, "0") == 0) return 0;
  if (std::strcmp(value, "1") == 0) return 1;
  return -1;
}

// Call only from an entry point that exclusively executes ordinary decode.
inline MoEPartitionLogPhase SingleDecodeLogPhase(int batch_size) {
  return batch_size == 1 ? MoEPartitionLogPhase::kSingleDecode
                         : MoEPartitionLogPhase::kUnknown;
}

inline bool ShouldEmitMoEPartitionLog(int partition_mode, int quiet_mode,
                                      MoEPartitionLogPhase phase, int tokens,
                                      bool has_prefill_request) {
  return partition_mode == 1 &&
         !(quiet_mode == 1 && phase == MoEPartitionLogPhase::kSingleDecode &&
           tokens == 1 && !has_prefill_request);
}

// Cached per process. Reports its configuration once; -1 means invalid input.
int MoEDecodePartitionLogQuietMode();

}  // namespace q4t::model
