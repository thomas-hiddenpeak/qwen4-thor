// Live audio events from verified native 24k PCM; no model or rate owner.
#pragma once

#include "q4t/server/live_session.h"
#include "q4t/server/realtime_speech_execution.h"

namespace q4t::server {
class LiveAudioOutput {
 public:
  LiveAudioOutput(RealtimeAudioFormat format, uint64_t begin_ticks,
                  LiveEventSink emit, std::function<bool()> cancelled = {},
                  std::string client_event = {});
  bool Valid() const { return valid_; }
  ObjectReply Append(const RealtimeSpeechEvent& event);
  void Cancel() noexcept { cancelled_ = true; }
  bool Completed() const { return completed_; }
  uint64_t EndTicks() const { return begin_ticks_ + output_ticks_; }
  uint64_t Bytes() const { return output_bytes_; }
  const ObjectReply& Error() const { return error_; }

 private:
  ObjectReply Write(std::string_view bytes);
  ObjectReply Emit(io::Json event);
  ObjectReply Fail(std::string_view message);
  const RealtimeAudioFormat format_;
  const uint64_t begin_ticks_;
  const LiveEventSink emit_;
  const std::function<bool()> interrupted_;
  const std::string client_event_;
  RealtimeSpeechEncoder encoder_;
  uint64_t input_bytes_ = 0, output_bytes_ = 0, output_ticks_ = 0;
  bool valid_ = false, completed_ = false, cancelled_ = false;
  ObjectReply error_;
};
}  // namespace q4t::server
