// Independent resource, health and literal prompt contract for alignment.
#pragma once

#include <functional>
#include <span>
#include <vector>

#include "q4t/audio/aligner_postprocess.h"
#include "q4t/server/asr_service.h"

namespace q4t::server {
struct AlignerServiceLimits {
  int max_audio_seconds = 30;
  int max_context_tokens = 4096;
  int max_words = 512;
  int workspace_mib = 1024;
  bool Valid() const {
    // 5000 trained classes at 80ms cannot describe audio beyond 400 seconds.
    // This is a minimum context bound; actual BPE length is checked per call.
    return max_audio_seconds >= 1 && max_audio_seconds <= 400 &&
           max_context_tokens >= 128 && max_context_tokens <= 8192 &&
           max_words >= 1 && max_words <= 2048 && workspace_mib >= 64 &&
           workspace_mib <= 8192 &&
           max_audio_seconds * 13 + 2 + max_words * 3 <= max_context_tokens;
  }
};
using AlignerResourcePlan = AsrResourcePlan;
Status PlanAlignerServiceResources(const AlignerServiceLimits& limits,
                                   AlignerResourcePlan* out);
std::string AlignerServiceLimitsJson(const AlignerServiceLimits& limits);
const char* AlignerFailureCode(AsrFailure failure);
std::string AlignerHealthJson(const AsrHealthSnapshot& state, bool gpu_healthy);

struct AlignerPrompt {
  std::vector<int32_t> tokens;
  std::vector<int32_t> positions;
  size_t audio_tokens = 0;
};
// Callback must encode literal text, with special-token recognition disabled.
// Every word must produce literal IDs; timestamp markers are inserted here.
using AlignerEncode =
    std::function<Status(std::string_view, std::vector<uint32_t>*)>;
Status BuildAlignerPrompt(std::span<const audio::AlignerWord> words,
                          size_t audio_tokens,
                          const AlignerServiceLimits& limits,
                          const AlignerEncode& encode, AlignerPrompt* out,
                          const std::function<bool()>& cancelled = {});
}  // namespace q4t::server
