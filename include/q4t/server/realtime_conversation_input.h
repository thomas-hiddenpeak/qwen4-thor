// Native input processing inside one conversation owner; no second socket.
#pragma once

#include <map>

#include "q4t/server/realtime_conversation.h"
#include "q4t/server/realtime_duplex_input.h"
#include "q4t/server/realtime_transcription_session.h"

namespace q4t::server {
struct RealtimeInputCommit {
  RealtimeItemTicket ticket;
  RealtimeDuplexInput settings;
  bool automatic = false;
  // Frozen at actual commit, before later client events or output changes.
  // Present only when this turn requests an automatic response.
  RealtimeConversationSnapshot context;
  ObjectReply context_status;
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
};
// At most 32 queued commit notices, each capped by the 16MiB history limit.
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
  bool Tick();
  bool Owns(const RealtimeItemTicket& ticket) const;
  ObjectReply Retrieve(const RealtimeItemTicket& ticket, io::Json* item) const;
  ObjectReply Erase(const RealtimeItemTicket& ticket);
  void Cancel() noexcept;
  void Join() noexcept;
  bool Failed() const { return failed_; }
  size_t PendingItems() const { return processor_.QueuedItems(); }
  size_t AudioBytes() const { return processor_.AudioBytes(); }
  size_t RetainedAudioBytes() const { return processor_.RetainedAudioBytes(); }

 private:
  struct Binding {
    RealtimeTranscriptTicket input;
    RealtimeItemTicket conversation;
    std::shared_ptr<RealtimeAudioRecord> audio;
    RealtimeDuplexInput settings;
    std::optional<RealtimeInputCommit> notice;
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
  bool Drain();
  bool Forward(io::Json event);
  bool ErrorEvent(ObjectReply reply, std::string_view event_id = {});
  bool StopInput();
  void FailPending() noexcept;
  RealtimeConversation& history_;
  const RealtimeConversationInputCallbacks callbacks_;
  const size_t message_bytes_;
  std::function<bool()> interrupted_;
  RealtimeDuplexInput settings_;
  RealtimeAudioFormat retrieval_format_;
  std::map<std::string, Binding> bindings_;
  WebSocketOutput output_{kWebSocketOutputMaximumBytes, 64,
                          kWebSocketMaximumLimit};
  WebSocketDecoder decoder_{false, kWebSocketMaximumLimit};
  RealtimeTranscriptionSession processor_;
  bool started_ = false, failed_ = false, cancelled_ = false;
};
}  // namespace q4t::server
