// Native input processing inside one conversation owner; no second socket.
#pragma once

#include <map>

#include "q4t/server/realtime_conversation.h"
#include "q4t/server/realtime_duplex_input.h"
#include "q4t/server/realtime_prompt_settings.h"
#include "q4t/server/realtime_transcription_session.h"

namespace q4t::server {
struct RealtimeInputCommit {
  RealtimeItemTicket ticket;
  RealtimeAudioItem audio;
  RealtimeDuplexInput settings;
  bool automatic = false;
  // Frozen at actual commit, before later client events or output changes.
  // Present only when this turn requests an automatic response.
  RealtimeConversationSnapshot context;
  ObjectReply context_status;
  std::optional<RealtimePromptSelection> response;
};
struct RealtimeConversationInputCallbacks {
  std::function<ObjectReply(const io::Json&)> emit;
  std::function<bool()> ready;
  std::function<bool(bool started, std::string_view id, uint64_t ticks,
                     bool interrupt)>
      speech;
  // Called after the public input commit and item.done, exactly once. The
  // caller schedules a new response, using the already frozen context.
  std::function<ObjectReply(RealtimeInputCommit)> committed;
  // Same-owner bounded copy at actual commit, before deferred public events.
  std::function<ObjectReply(RealtimePromptSelection*)> capture_response;
  std::function<std::chrono::steady_clock::time_point()> now;
  // Optional native transcript observer with processor-owned sample ranges.
  // Runs on the same owner before the ordinary public callback. No new wire
  // fields; a client cannot forge these ranges through event JSON.
  std::function<ObjectReply(const io::Json&, const RealtimeAudioItem&)>
      transcript;
};
// At most four response captures, each <=96MiB including history, effective
// settings and pinned prompt. Metadata/settings retain the input queue bound.
// Actual audio and all outstanding borrowers keep the existing input budget.
inline constexpr size_t kRealtimeConversationInputHostBytes =
    kRealtimeSessionHostBytes + (512ULL << 20);

class RealtimeConversationInput final : private IRealtimeTranscriptionOwner {
 public:
  RealtimeConversationInput(std::string id, RealtimeConversation& history,
                            RealtimeTranscriptionCallbacks native,
                            RealtimeConversationInputCallbacks callbacks,
                            uint32_t max_audio_seconds,
                            size_t message_bytes = kWebSocketMessageLimit);
  ~RealtimeConversationInput();
  // Same-owner configuration; no extra session acknowledgement. Effective
  // settings are copied before the processor transaction commits.
  ObjectReply Configure(RealtimeDuplexInput settings);
  bool Message(std::string_view message);
  bool Tick(bool allow_work = true);
  bool Owns(const RealtimeItemTicket& ticket) const;
  ObjectReply Retrieve(const RealtimeItemTicket& ticket, io::Json* item) const;
  ObjectReply Erase(const RealtimeItemTicket& ticket);
  // One mixed item, with independently processed audio parts. False selects a
  // request-owned temporary item, excluded from the default conversation.
  ObjectReply CreateDirect(const io::Json& item, std::string_view generated_id,
                           std::optional<std::string_view> previous,
                           bool in_conversation, RealtimeItemTicket* output);
  ObjectReply PublicItem(const RealtimeItemTicket& ticket,
                         io::Json* item) const;
  void Cancel() noexcept;
  void Join() noexcept;
  bool Failed() const { return failed_; }
  size_t PendingItems() const { return processor_.QueuedItems(); }
  bool Executing() const { return processor_.Executing(); }
  size_t AudioBytes() const { return processor_.AudioBytes(); }
  size_t RetainedAudioBytes() const { return processor_.RetainedAudioBytes(); }
  uint64_t AcceptedTicks() const { return processor_.AcceptedTicks(); }
  size_t UncommittedBytes() const { return processor_.UncommittedBytes(); }
  RealtimeInputTail UncommittedTail() const {
    return processor_.UncommittedTail();
  }
  bool Drained() const {
    return !failed_ && !processor_.QueuedItems() && !processor_.Executing() &&
           !processor_.UncommittedBytes() && !processor_.PendingOutput();
  }
  // Default-conversation output only. Playback is actual emitted sample time.
  void ResponseFinished(uint64_t playback_ticks);
  void CancelIdle() noexcept;

 private:
  struct Binding {
    RealtimeTranscriptTicket input;
    RealtimeAudioItem input_audio;
    RealtimeItemTicket conversation;
    std::shared_ptr<RealtimeAudioRecord> audio;
    RealtimeDuplexInput settings;
    std::optional<RealtimeInputCommit> notice;
    size_t content_index = 0;
    bool finite = false;
    std::optional<std::string> reference_transcript;
    bool captured = false;
    bool committed = false, settled = false, published = false;
  };
  RealtimeTranscriptionCallbacks Bind(RealtimeTranscriptionCallbacks native);
  ObjectReply Reserve(const RealtimeTranscriptionJob& job) override;
  ObjectReply Commit(const RealtimeTranscriptionJob& job, bool automatic,
                     std::string* previous_id) override;
  ObjectReply Finish(const RealtimeTranscriptionJob& job,
                     const ObjectReply& result) override;
  void Discard(const RealtimeTranscriptionJob& job) noexcept override;
  bool Speech(bool started, std::string_view id, uint64_t ticks) override;
  bool Ready(const RealtimeTranscriptionJob& job) override;
  void ReleaseNotice(Binding& binding);
  void ReleaseBinding(Binding& binding);
  bool Drain();
  bool Forward(io::Json event);
  bool ErrorEvent(ObjectReply reply, std::string_view event_id = {});
  bool StopInput();
  ObjectReply RecoverInput(RealtimeDuplexInput next);
  std::chrono::steady_clock::time_point Now() const;
  void ArmIdle();
  void StartIdleCapture();
  bool TickIdle();
  void FailPending() noexcept;
  RealtimeConversation& history_;
  const RealtimeConversationInputCallbacks callbacks_;
  const size_t message_bytes_;
  std::function<bool()> interrupted_;
  RealtimeDuplexInput settings_;
  RealtimeAudioFormat retrieval_format_;
  std::map<std::string, Binding> bindings_;
  const RealtimeItemTicket* creating_direct_ = nullptr;
  size_t reference_bytes_ = 0;
  static constexpr size_t kReferenceBytes = 16ULL << 20;
  size_t captures_ = 0;
  std::chrono::steady_clock::time_point recovery_window_, retry_after_;
  unsigned recovery_attempts_ = 0;
  std::optional<std::chrono::steady_clock::time_point> playback_end_;
  std::optional<std::chrono::steady_clock::time_point> idle_deadline_;
  bool idle_capturing_ = false;
  WebSocketOutput output_{kWebSocketOutputMaximumBytes, 64,
                          kWebSocketMaximumLimit};
  WebSocketDecoder decoder_{false, kWebSocketMaximumLimit};
  RealtimeTranscriptionSession processor_;
  bool started_ = false, failed_ = false, cancelled_ = false;
};
}  // namespace q4t::server
