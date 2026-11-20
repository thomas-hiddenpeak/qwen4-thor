// Session-local conversation ownership; no model, socket or storage service.
#pragma once

#include <cstdint>
#include <optional>
#include <span>
#include <string>
#include <string_view>
#include <vector>

#include "q4t/io/json.h"

namespace q4t::server {
// Actual allocated JSON capacities, with the same depth/node bound as history.
Status MeasureRealtimeStorage(const io::Json& value, size_t maximum,
                              size_t* bytes);
struct RealtimeItemTicket {
  std::string owner, id;
  uint64_t generation = 0;
  bool operator==(const RealtimeItemTicket&) const = default;
};
struct RealtimeConversationSnapshot {
  std::string owner;
  uint64_t revision = 0;
  io::Json items;
  std::vector<RealtimeItemTicket> tickets = {};
  bool default_context = false;
};

// One session thread owns mutations. Workers receive independent snapshots
// and tickets; they never hold references into this ledger. Out-of-band items
// are retrievable by session-local ID but do not enter the default context.
class RealtimeConversation {
 public:
  static constexpr size_t kMaximumItems = 128;
  static constexpr size_t kMaximumBytes = 16ULL << 20;
  explicit RealtimeConversation(std::string owner,
                                size_t maximum_items = kMaximumItems,
                                size_t maximum_bytes = kMaximumBytes);
  RealtimeConversation(const RealtimeConversation&) = delete;
  RealtimeConversation& operator=(const RealtimeConversation&) = delete;
  bool Valid() const { return valid_; }
  size_t Items() const { return entries_.size(); }
  // Bounds retained JSON storage (including capacities) and pending output
  // reservations. Fixed ledger metadata is bounded by maximum_items. Caller
  // also reserves space for transaction copies and every worker snapshot.
  size_t ChargedBytes() const { return charged_; }
  uint64_t Revision() const { return revision_; }
  std::string LastId() const;
  Status Find(std::string_view id, RealtimeItemTicket* output) const;
  Status Get(const RealtimeItemTicket& ticket, io::Json* output) const;
  Status Erase(const RealtimeItemTicket& ticket);
  // No word/sample alignment is available. A valid playback truncation clears
  // the complete transcript, preserving allocated capacity and item identity.
  Status TruncateAudioTranscript(const RealtimeItemTicket& ticket);
  // Missing previous appends; "root" inserts first. An out-of-band or absent
  // predecessor is an error. Client-supplied status never hides valid history.
  Status Create(const io::Json& item, std::string_view generated_id,
                std::optional<std::string_view> previous,
                RealtimeItemTicket* output);
  // Reserve before publishing model output. Updates cannot grow beyond this
  // reservation or change identity; only a terminal update releases the slack.
  Status BeginOutput(const io::Json& item, std::string_view generated_id,
                     bool in_conversation, size_t reserved_bytes,
                     RealtimeItemTicket* output);
  // The native input processor reserves a user item before publishing its
  // audio commit. Public commit status is independent of pending ASR work.
  // Only these owned completion methods can make its text executable. A
  // client-created item/transcript never receives this provenance.
  Status BeginAudioInput(std::string_view id,
                         std::optional<std::string_view> previous,
                         bool transcript_visible, size_t reserved_bytes,
                         RealtimeItemTicket* output);
  Status CompleteAudioInput(const RealtimeItemTicket& ticket,
                            std::string_view transcript);
  Status FailAudioInput(const RealtimeItemTicket& ticket);
  Status Update(const RealtimeItemTicket& ticket, const io::Json& item,
                bool terminal, bool grow = false);
  // grow opts into atomic growth within this ledger's already bounded global
  // reservation. Existing fixed-reservation callers keep their old contract.
  // Null selects the default conversation; [] selects an empty context.
  // Explicit item_reference resolves only this session's completed entries.
  // Pending server output fails rather than silently dropping partial tools.
  // A terminal incomplete server tool remains retrievable but is not executable
  // context. Default snapshots omit it; an explicit reference fails. Client
  // status fields have no such authority and cannot hide submitted history.
  // Failure leaves output and history untouched; deleting an entry later does
  // not alter a snapshot already handed to a worker.
  Status Snapshot(const io::Json* input,
                  RealtimeConversationSnapshot* output) const;
  // Persist a successful default response's context cut without deleting its
  // retrievable history. Generation tickets protect entries recreated
  // meanwhile.
  Status TruncateDefault(const RealtimeConversationSnapshot& snapshot,
                         std::span<const size_t> retained);

 private:
  struct Entry {
    io::Json item;
    uint64_t generation = 0;
    size_t charge = 0;
    bool in_conversation = true, pending = false, server_output = false;
    bool in_context = true;
    bool native_input = false, transcript_visible = false, input_failed = false;
  };
  Status FinishAudioInput(const RealtimeItemTicket& ticket,
                          std::optional<std::string_view> transcript);
  Status UpdateEntry(const RealtimeItemTicket& ticket, const io::Json& item,
                     bool terminal, bool grow);
  size_t Position(const RealtimeItemTicket& ticket) const;
  Status Insert(const io::Json& item, std::string_view generated_id,
                std::optional<std::string_view> previous, bool in_conversation,
                size_t reserved_bytes, RealtimeItemTicket* output);
  const std::string owner_;
  const size_t maximum_items_, maximum_bytes_;
  bool valid_ = false;
  size_t charged_ = 0;
  uint64_t generation_ = 0, revision_ = 0;
  std::vector<Entry> entries_;
};
}  // namespace q4t::server
