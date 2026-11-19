#include "q4t/server/realtime_asr_policy.h"

#include <array>

namespace q4t::server {
RealtimeAsrPolicy RealtimeAsrDelayPolicy(std::string_view delay) {
  constexpr std::array<std::string_view, 5> names{"minimal", "low", "medium",
                                                  "high", "xhigh"};
  constexpr std::array<size_t, 5> samples{8000, 16000, 32000, 64000, 128000};
  constexpr std::array<size_t, 5> rollback{2, 3, 5, 7, 9};
  RealtimeAsrPolicy result;
  for (size_t i = 0; i < names.size(); ++i) {
    if (names[i] != delay) continue;
    result.chunk_samples = samples[i];
    result.prefix.rollback_tokens = rollback[i];
    break;
  }
  return result;
}
}  // namespace q4t::server
