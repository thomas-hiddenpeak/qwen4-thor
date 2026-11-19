// Online VAD uses actual continuous samples, never per-message clip scoring.
#pragma once

#include "q4t/audio/stream_resample.h"
#include "q4t/audio/vad.h"
#include "q4t/server/realtime_audio_queue.h"

namespace q4t::server {
struct RealtimeVadOptions {
  double threshold = .5;
  uint32_t prefix_ms = 300, silence_ms = 200;
};
struct RealtimeVadProgress {
  bool frame = false, started = false, stopped = false;
  uint64_t begin_ticks = 0, end_ticks = 0, trim_before = 0;
};
// Event-loop owned. One call consumes <=1024 input samples and scores <=one
// 512-sample frame. Immutable model weights may be shared; recurrent state,
// resampling phase and pending partial frame are strictly per connection.
class RealtimeVad {
 public:
  using Step =
      std::function<Status(std::span<const float>, audio::VadState*, float*)>;
  RealtimeVad(RealtimeAudioFormat format, uint64_t begin_ticks,
              RealtimeVadOptions options, Step step);
  bool Valid() const { return valid_; }
  Status Advance(const RealtimeAudioQueue& queue, RealtimeVadProgress* out);
  bool Active() const { return active_; }
  uint64_t CursorTicks() const;

 private:
  const RealtimeAudioFormat format_;
  const uint64_t origin_;
  const RealtimeVadOptions options_;
  const Step step_;
  audio::AsrStreamResampler resampler_;
  audio::VadState state_;
  std::array<float, audio::kVadFrameSamples> frame_{};
  size_t filled_ = 0;
  uint64_t frames_ = 0, speech_begin_ = 0, silence_begin_ = 0;
  bool valid_ = false, failed_ = false, active_ = false, silent_ = false;
};
}  // namespace q4t::server
