// Explicit local Qwen ASR delay policy, not remote model internal settings.
#pragma once

#include <string_view>

#include "q4t/server/asr_stream_state.h"

namespace q4t::server {
struct RealtimeAsrPolicy {
  size_t chunk_samples = 0;
  AsrStreamPrefixLimits prefix;
  bool Valid() const { return chunk_samples != 0; }
};
RealtimeAsrPolicy RealtimeAsrDelayPolicy(std::string_view delay);
}  // namespace q4t::server
