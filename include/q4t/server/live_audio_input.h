// Continuous Live input shares native ASR/VAD and the actual history owner.
#pragma once

#include "q4t/server/live_session.h"
#include "q4t/server/realtime_conversation_input.h"

namespace q4t::server {
struct LiveAudioInputCallbacks {
  LiveEventSink emit;
  std::function<bool()> ready;
  std::function<bool(bool, std::string_view, uint64_t, bool)> speech;
  std::function<ObjectReply(RealtimeInputCommit)> committed;
  std::function<ObjectReply(RealtimePromptSelection*)> capture_response;
};

class LiveAudioInput {
 public:
  LiveAudioInput(std::string id, RealtimeConversation& history,
                 RealtimeTranscriptionCallbacks native,
                 LiveAudioInputCallbacks callbacks, uint32_t max_audio_seconds,
                 size_t message_bytes = kWebSocketMessageLimit);
  ~LiveAudioInput();
  // Prepared before session.started, with no outward events or ASR generation.
  ObjectReply Prepare(RealtimeAudioFormat format);
  ObjectReply Append(std::string_view base64, std::string_view event_id = {});
  ObjectReply Mute(bool muted, std::string_view event_id = {});
  ObjectReply Tick();
  ObjectReply Close();
  bool Drained() const;
  void Cancel() noexcept;
  void Join() noexcept;
  uint64_t AcceptedTicks() const { return input_.AcceptedTicks(); }
  size_t AudioBytes() const { return input_.AudioBytes(); }
  bool Executing() const { return input_.Executing(); }
  size_t PendingItems() const { return input_.PendingItems(); }

 private:
  RealtimeConversationInputCallbacks Bind();
  ObjectReply Forward(const io::Json& event);
  ObjectReply Transcript(const io::Json& event, const RealtimeAudioItem& audio);
  ObjectReply Command(io::Json event);
  ObjectReply FinishTail();
  ObjectReply ErrorEvent(ObjectReply reply, std::string_view event_id = {});
  ObjectReply Fail();
  const LiveAudioInputCallbacks callbacks_;
  const size_t message_bytes_;
  RealtimeConversationInput input_;
  RealtimeAudioFormat format_;
  ObjectReply command_error_;
  std::string transcript_id_, transcript_text_;
  uint8_t pending_byte_ = 0;
  bool prepared_ = false, closing_ = false, cancelled_ = false, failed_ = false;
  bool muted_ = false, partial_ = false, partial_muted_ = false;
  bool in_command_ = false;
};
}  // namespace q4t::server
