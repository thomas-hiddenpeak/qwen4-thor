// Independent resources and health for optional native two-source separation.
#pragma once

#include "q4t/server/asr_service.h"

namespace q4t::server {
struct SeparatorServiceLimits {
  int max_audio_seconds = 30;
  int workspace_mib = 2048;
  bool Valid() const {
    return max_audio_seconds >= 1 && max_audio_seconds <= 30 &&
           workspace_mib >= 64 && workspace_mib <= 2048;
  }
};
struct SeparatorResourcePlan {
  uint64_t request_device_bytes = 0;
  uint64_t request_host_bytes = 0;
  uint64_t resident_weight_bytes = 0;
  uint64_t load_peak_bytes = 0;
};
// Includes both restored tracks and intermediate host PCM. ASR, VAD, speaker
// and aligner allocations remain charged to their owners in shared DRAM.
// Explicit allocation estimate, not a physical reservation/library peak.
Status PlanSeparatorServiceResources(const SeparatorServiceLimits& limits,
                                     SeparatorResourcePlan* out);
std::string SeparatorServiceLimitsJson(const SeparatorServiceLimits& limits);
const char* SeparatorFailureCode(AsrFailure failure);
std::string SeparatorHealthJson(const AsrHealthSnapshot& state,
                                bool gpu_healthy);
}  // namespace q4t::server
