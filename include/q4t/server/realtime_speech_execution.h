// Native speech delivery on the original Realtime response worker.
#pragma once

#include "q4t/server/chat_audio_output.h"
#include "q4t/server/realtime_speech_encoder.h"
#include "q4t/server/speech_output.h"
#include "q4t/trace/sha256.h"

namespace q4t::server {
struct RealtimeSpeechOptions {
  bool enabled = false;
  RealtimeAudioFormat format;
  ChatAudioOptions chat;
};
// Merge per-response output options with effective session settings. Input
// audio is owned separately. No lookup, voice substitution or model execution.
Status PlanRealtimeSpeech(const io::Json& settings, const io::Json& response,
                          RealtimeSpeechOptions* out, ChatPlanError* error);
bool SameRealtimeVoice(const RealtimeSpeechOptions& left,
                       const RealtimeSpeechOptions& right);
struct RealtimeSpeechEvent {
  enum class Kind { kChunk, kDone } kind = Kind::kChunk;
  std::string bytes, transcript;
  RealtimeSpeechReceipt receipt;
};
using RealtimeSpeechSink =
    std::function<ObjectReply(const RealtimeSpeechEvent&)>;

// Verifies actual post-tempo PCM against the native EOF receipt before
// publishing completion. No waveform accumulation or extra RPM/model owner.
class RealtimeSpeechExecution {
 public:
  RealtimeSpeechExecution(const RealtimeSpeechOptions& options,
                          RealtimeSpeechSink sink,
                          std::function<bool()> cancelled);
  bool Valid() const { return valid_; }
  bool Completed() const { return completed_; }
  const std::optional<ActualUsage>& Usage() const { return usage_; }
  ObjectReply Append(std::string_view pcm);
  ObjectReply Finish(std::string_view transcript,
                     const SpeechEncodingReceipt& native,
                     const ActualUsage& usage);

 private:
  ObjectReply Fail(std::string_view message);
  ObjectReply Emit(const RealtimeSpeechEvent& event);
  RealtimeSpeechEncoder encoder_;
  trace::Sha256Stream hash_;
  const RealtimeSpeechSink sink_;
  const std::function<bool()> cancelled_;
  bool valid_ = false, completed_ = false;
  std::optional<ActualUsage> usage_;
  ObjectReply error_;
};
}  // namespace q4t::server
