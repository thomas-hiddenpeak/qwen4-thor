// Actual generated wire audio, owned by one Realtime session thread.
#pragma once

#include "q4t/server/realtime_audio_queue.h"
#include "q4t/server/realtime_conversation.h"

namespace q4t::server {
struct RealtimeSpeechView {
  RealtimeAudioFormat format;
  std::string_view bytes;
  uint64_t generated_samples = 0;
  bool complete = false, truncated = false, interrupted = false;
};

// Views are synchronous borrows: no worker may retain one across a mutation.
// Reserve binary capacity before publishing the first chunk. JSON/transcript,
// metadata, retrieval encoding and transient construction have separate bounds
// in the session owner. Truncation retains its allocation and charge until
// deletion; the generated extent remains distinct from the retained waveform.
class RealtimeSpeechHistory {
 public:
  static constexpr size_t kMaximumBytes = 128ULL << 20;
  explicit RealtimeSpeechHistory(std::string owner,
                                 size_t maximum_bytes = kMaximumBytes,
                                 size_t maximum_items = 128);
  RealtimeSpeechHistory(const RealtimeSpeechHistory&) = delete;
  RealtimeSpeechHistory& operator=(const RealtimeSpeechHistory&) = delete;
  bool Valid() const { return valid_; }
  size_t ChargedBytes() const { return charged_; }
  size_t Items() const { return entries_.size(); }
  Status Begin(const RealtimeItemTicket& ticket, RealtimeAudioFormat format,
               size_t maximum_record_bytes);
  Status Append(const RealtimeItemTicket& ticket, std::string_view bytes);
  Status Complete(const RealtimeItemTicket& ticket);
  Status Interrupt(const RealtimeItemTicket& ticket);
  // WebSocket clients supply their actual played duration. It must not exceed
  // generated audio. Calling this seals the record against all late output.
  Status Truncate(const RealtimeItemTicket& ticket, uint64_t audio_end_ms);
  Status Get(const RealtimeItemTicket& ticket, RealtimeSpeechView* view) const;
  Status Erase(const RealtimeItemTicket& ticket);

 private:
  struct Entry {
    RealtimeItemTicket ticket;
    RealtimeAudioFormat format;
    std::string bytes;
    size_t maximum = 0;
    uint64_t generated_samples = 0;
    bool complete = false, truncated = false, interrupted = false;
  };
  size_t Position(const RealtimeItemTicket& ticket) const;
  const std::string owner_;
  const size_t maximum_bytes_, maximum_items_;
  bool valid_ = false;
  size_t charged_ = 0;
  std::vector<Entry> entries_;
};
}  // namespace q4t::server
