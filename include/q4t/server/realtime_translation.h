// Effective settings shared by native translation credentials and sessions.
#pragma once

#include "q4t/server/realtime_shape.h"
#include "q4t/server/object_store.h"
#include "q4t/audio/noise_reduction.h"

namespace q4t::server {
inline constexpr std::string_view kRealtimeTranslationModel =
    "gpt-realtime-translate";
inline constexpr std::string_view kRealtimeTranslationAsr =
    "gpt-realtime-whisper";

struct RealtimeTranslationSettings {
  std::string language = "en", transcription;
  audio::NoiseReduction noise = audio::NoiseReduction::kOff;
  double strength = 1;

  // Omitted settings survive an update. Null explicitly clears a processor.
  ObjectReply Apply(const io::Json& configuration, bool create);
  io::Json Resource(std::string_view id, int64_t expires_at) const;
};
}  // namespace q4t::server
