// Independent limits/resource/health contract for optional speaker inference.
#pragma once

#include "q4t/server/asr_service.h"

namespace q4t::server {
struct SpeakerServiceLimits {
  int window_ms = 3000;
  int step_ms = 750;
  int max_windows = 256;
  int max_speakers = 16;
  int workspace_mib = 128;
  bool Valid() const {
    return window_ms >= 1000 && window_ms <= 30000 && step_ms >= 100 &&
           step_ms <= window_ms && max_windows >= 1 && max_windows <= 1024 &&
           max_speakers >= 1 && max_speakers <= 64 && workspace_mib >= 16 &&
           workspace_mib <= 1024;
  }
};
struct SpeakerResourcePlan {
  uint64_t request_device_bytes = 0;
  uint64_t request_host_bytes = 0;
  uint64_t resident_weight_bytes = 0;
  uint64_t load_peak_bytes = 0;
};
// Own explicit allocations plus bounded request/staging overhead. Shares the
// same physical RAM with Chat/ASR/aligner; callers must add their live totals.
// This estimate is not a reservation or a measurement of library internals.
Status PlanSpeakerServiceResources(const SpeakerServiceLimits& limits,
                                   SpeakerResourcePlan* out);
std::string SpeakerServiceLimitsJson(const SpeakerServiceLimits& limits);
const char* SpeakerFailureCode(AsrFailure failure);
std::string SpeakerHealthJson(const AsrHealthSnapshot& state, bool gpu_healthy);
}  // namespace q4t::server
