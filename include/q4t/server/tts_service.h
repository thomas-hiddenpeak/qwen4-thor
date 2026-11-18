// Unified-RAM admission and independent health for the native speech backend.
#pragma once

#include "q4t/audio/tts_config.h"
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
  uint64_t reference_host_bytes = 0;
  uint64_t resident_weight_bytes = 0;
  uint64_t resident_host_bytes = 0;
  uint64_t source_staging_bytes = 0;
  uint64_t possible_file_page_bytes = 0;
  uint64_t expansion_scratch_bytes = 0;
  uint64_t load_peak_bytes = 0;
  uint64_t encoder_address_space_limit = 0;
};

// Exact request dimensions are used for admission; no silent token/frame
// trimming. Direct-read source staging, F32 device weights and expansion
// scratch coexist in physical DRAM. This is an allocation estimate, not a
// reservation or measured RSS. Other backends remain charged to their owners.
Status PlanTtsServiceResources(
    const TtsServiceLimits& limits, size_t text_tokens, size_t prefill_tokens,
    size_t max_frames, TtsResourcePlan* out,
    audio::TtsVariant variant = audio::TtsVariant::kCustomVoice17B,
    size_t reference_frames = 0);
struct TtsVoiceResourcePlan {
  uint64_t frames = 0, device_weight_bytes = 0, device_workspace_bytes = 0;
  uint64_t host_feature_bytes = 0, source_staging_bytes = 0;
  uint64_t load_peak_bytes = 0;
};
// Registration loads no talker or codec device graph. Complete Base source is
// still verified/staged, and all PCM, frontend, embedding and FP32 storage is
// charged to the same physical RAM. Caller separately accounts upload/decoder.
Status PlanTtsVoiceResources(size_t pcm_samples, size_t max_workspace_bytes,
                             TtsVoiceResourcePlan* out);
std::string TtsServiceLimitsJson(const TtsServiceLimits& limits);
const char* TtsFailureCode(AsrFailure failure);
// The TTS service owns its own AsrHealth, never the ASR/core instance.
std::string TtsHealthJson(const AsrHealthSnapshot& state, bool gpu_healthy);
}  // namespace q4t::server
