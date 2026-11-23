// Converts between the carrier's PCM16/24 kHz and a Realtime audio stream.
#pragma once

#include "q4t/audio/stream_resample.h"
#include "q4t/server/realtime_audio_queue.h"

#include <string>
#include <string_view>

namespace q4t::server {
class RealtimeCallPcm {
 public:
  RealtimeCallPcm(RealtimeAudioFormat from, RealtimeAudioFormat to);
  bool Convert(std::string_view input, std::string* output,
               bool finish = false);

 private:
  const RealtimeAudioFormat from_, to_;
  audio::AsrStreamResampler resampler_;
  bool finished_ = false;
};
}  // namespace q4t::server
