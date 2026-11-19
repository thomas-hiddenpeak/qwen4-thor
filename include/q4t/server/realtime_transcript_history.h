// Session-owned actual ASR context. No storage service or model dependency.
#pragma once

#include <cstddef>
#include <cstdint>
#include <optional>
#include <string>
#include <string_view>
#include <vector>

#include "q4t/status.h"

namespace q4t::server {
inline constexpr size_t kRealtimeTranscriptHistoryBytes = 128ULL << 20;
struct RealtimeTranscriptTicket {
  std::string owner, id;
  uint64_t generation = 0;
  bool operator==(const RealtimeTranscriptTicket&) const = default;
};

// One session owner mutates this ledger. Workers receive independent copies
// of ContextBefore and tickets, never references into its storage. The caller
// supplies its unique session identity and reserves the bounded host envelope.
// Audio buffers and general conversation items remain separate owners; this
// component records only native transcription provenance for those item IDs.
class RealtimeTranscriptHistory {
 public:
  explicit RealtimeTranscriptHistory(std::string owner,
                                     size_t maximum_items = 128,
                                     size_t transcript_bytes = 1ULL << 20);
  RealtimeTranscriptHistory(const RealtimeTranscriptHistory&) = delete;
  RealtimeTranscriptHistory& operator=(const RealtimeTranscriptHistory&) =
      delete;
  bool Valid() const { return valid_; }
  size_t Items() const { return entries_.size(); }
  // Actual completed text + still-pending output reservations. Item/ID
  // overhead has the independent fixed item bound; this is not physical RAM.
  size_t ChargedBytes() const { return charged_; }
  Status Begin(std::string_view id, size_t maximum_transcript_bytes,
               RealtimeTranscriptTicket* output);
  Status Commit(const RealtimeTranscriptTicket& ticket);
  // Must follow both an input commit and successful actual ASR completion.
  // Empty successful text is legitimate; it is not invented for a failure.
  Status Complete(const RealtimeTranscriptTicket& ticket,
                  std::string_view transcript);
  Status Fail(const RealtimeTranscriptTicket& ticket);
  Status Erase(const RealtimeTranscriptTicket& ticket);
  Status Find(std::string_view id, RealtimeTranscriptTicket* output) const;
  bool Committed(const RealtimeTranscriptTicket& ticket) const;
  std::string LastCommittedId() const;
  // Owner-thread view, valid until the next ledger mutation. A committed
  // pending/failed turn has no transcript; successful silence is an empty one.
  Status Transcript(const RealtimeTranscriptTicket& ticket,
                    std::optional<std::string_view>* output) const;
  // Preserves output on failure, never truncates or skips pending earlier
  // committed work. Failed/cleared/empty turns add no context. The current
  // turn and all later turns are excluded, even if they already completed.
  Status ContextBefore(const RealtimeTranscriptTicket& ticket,
                       size_t maximum_bytes, std::string* output) const;

 private:
  enum class State { kPending, kCompleted, kFailed };
  struct Entry {
    std::string id, text;
    uint64_t generation = 0;
    size_t charge = 0;
    State state = State::kPending;
    bool committed = false;
  };
  size_t Position(const RealtimeTranscriptTicket& ticket) const;
  const std::string owner_;
  const size_t maximum_items_, maximum_bytes_;
  bool valid_ = false;
  size_t charged_ = 0;
  uint64_t generation_ = 0;
  std::vector<Entry> entries_;
};
}  // namespace q4t::server
