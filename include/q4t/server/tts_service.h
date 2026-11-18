// Unified-RAM admission and independent health for the native speech backend.
#pragma once

#include "q4t/server/asr_service.h"

namespace q4t::server {
struct TtsServiceLimits {
  int max_text_tokens =
      16384;  // Text and instruction, including prompt tokens.
  int max_prefill_tokens = 8192;
  int max_frames = 4096;
  int workspace_mib = 8192;
  int max_output_mib = 64;
  bool Valid() const;
};
struct TtsResourcePlan {
  uint64_t generation_bytes = 0;
  uint64_t codec_bytes = 0;
  uint64_t request_device_bytes = 0;
  uint64_t request_host_bytes = 0;
  uint64_t resident_weight_bytes = 0;
  uint64_t resident_host_bytes = 0;
  uint64_t source_staging_bytes = 0;
  uint64_t possible_file_page_bytes = 0;
  uint64_t expansion_scratch_bytes = 0;
  uint64_t load_peak_bytes = 0;
  uint64_t encoder_address_space_limit = 0;
};

// Exact request dimensions are used for admission; no silent token/frame
// trimming. File pages, source staging, F32 device weights and expansion
// scratch coexist in physical DRAM. This is an allocation estimate, not a
// reservation or measured RSS. Other backends remain charged to their owners.
Status PlanTtsServiceResources(const TtsServiceLimits& limits,
                               size_t text_tokens, size_t prefill_tokens,
                               size_t max_frames, TtsResourcePlan* out);
std::string TtsServiceLimitsJson(const TtsServiceLimits& limits);
const char* TtsFailureCode(AsrFailure failure);
// The TTS service owns its own AsrHealth, never the ASR/core instance.
std::string TtsHealthJson(const AsrHealthSnapshot& state, bool gpu_healthy);
}  // namespace q4t::server
