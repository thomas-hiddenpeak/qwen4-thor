// One validated finite PCM fixture, paced on the native session owner.
#pragma once

#include "q4t/server/live_recording.h"

#include "q4t/server/live_session.h"
#include "q4t/server/realtime_speech_encoder.h"

namespace q4t::server {
class LiveAudioFixture {
 public:
  using Clock = std::chrono::steady_clock;
  static constexpr size_t kMaximumBytes = 30 * 24000 * 2;
  LiveAudioFixture(RealtimeAudioFormat format, LiveEventSink emit,
                   std::function<Clock::time_point()> now = {},
                   LiveRecordSink record = {});
  // Entire validation precedes owner interruption, mutation and acceptance.
  ObjectReply Prepare(std::string_view audio, std::string client_event);
  ObjectReply Accept();
  // Call only after prior model/ASR borrowers actually exit. At most 20ms of
  // source audio per tick; completion is server drain, not listener receipt.
  ObjectReply Tick(uint64_t timeline);
  void Cancel() noexcept;
  bool Completed() const { return completed_; }
  uint64_t EndTicks() const { return begin_ticks_ + input_bytes_; }
  size_t Bytes() const { return output_bytes_; }

 private:
  ObjectReply Emit(io::Json event);
  ObjectReply Write(std::string_view bytes);
  ObjectReply Fail(ObjectReply reply);
  const RealtimeAudioFormat format_;
  const LiveEventSink emit_;
  const LiveRecordSink record_;
  const std::function<Clock::time_point()> now_;
  RealtimeSpeechEncoder encoder_;
  std::string pcm_, client_event_;
  size_t input_bytes_ = 0, position_ = 0, output_bytes_ = 0;
  uint64_t begin_ticks_ = 0;
  Clock::time_point started_, observed_;
  ObjectReply error_;
  bool prepared_ = false, accepted_ = false, started_clock_ = false;
  bool cancelled_ = false, completed_ = false;
};
}  // namespace q4t::server
