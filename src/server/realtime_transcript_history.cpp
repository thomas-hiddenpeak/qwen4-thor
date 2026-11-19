#include "q4t/server/realtime_transcript_history.h"

#include <algorithm>
#include <limits>

#include "q4t/server/asr_response.h"
#include "q4t/server/http_response.h"
#include "q4t/server/input_contract.h"

namespace q4t::server {
namespace {
Status Invalid() {
  return Status::Fail("Realtime transcript history identity or state invalid");
}
}  // namespace

RealtimeTranscriptHistory::RealtimeTranscriptHistory(std::string owner,
                                                     size_t maximum_items,
                                                     size_t transcript_bytes)
    : owner_(std::move(owner)),
      maximum_items_(maximum_items),
      maximum_bytes_(transcript_bytes) {
  valid_ = ValidRequestId(owner_) && maximum_items_ && maximum_items_ <= 128 &&
           maximum_bytes_ && maximum_bytes_ <= kRealtimeTranscriptHistoryBytes;
  if (valid_) entries_.reserve(maximum_items_);
}
size_t RealtimeTranscriptHistory::Position(
    const RealtimeTranscriptTicket& ticket) const {
  if (valid_ && ticket.owner == owner_ && ticket.generation)
    for (size_t i = 0; i < entries_.size(); ++i)
      if (entries_[i].id == ticket.id &&
          entries_[i].generation == ticket.generation)
        return i;
  return entries_.size();
}
Status RealtimeTranscriptHistory::Find(std::string_view id,
                                       RealtimeTranscriptTicket* output) const {
  if (!valid_ || !output) return Invalid();
  for (const auto& entry : entries_) {
    if (entry.id != id) continue;
    RealtimeTranscriptTicket ticket{owner_, entry.id, entry.generation};
    *output = std::move(ticket);
    return {};
  }
  return Invalid();
}
Status RealtimeTranscriptHistory::Begin(std::string_view id,
                                        size_t maximum_transcript_bytes,
                                        RealtimeTranscriptTicket* output) {
  if (!valid_ || !output || !ValidRequestId(id) || id == "root" ||
      !maximum_transcript_bytes ||
      maximum_transcript_bytes > kAsrTranscriptBytes ||
      entries_.size() == maximum_items_ ||
      maximum_transcript_bytes > maximum_bytes_ - charged_ ||
      generation_ == std::numeric_limits<uint64_t>::max() ||
      std::ranges::any_of(entries_,
                          [&](const auto& entry) { return entry.id == id; }))
    return Invalid();
  // All potentially allocating copies precede the visible state mutation.
  Entry entry;
  entry.id = id;
  entry.charge = maximum_transcript_bytes;
  entry.generation = generation_ + 1;
  RealtimeTranscriptTicket ticket{owner_, entry.id, entry.generation};
  entries_.push_back(std::move(entry));
  charged_ += maximum_transcript_bytes;
  ++generation_;
  *output = std::move(ticket);
  return {};
}
bool RealtimeTranscriptHistory::Committed(
    const RealtimeTranscriptTicket& ticket) const {
  const auto index = Position(ticket);
  return index != entries_.size() && entries_[index].committed;
}
std::string RealtimeTranscriptHistory::LastCommittedId() const {
  for (auto it = entries_.rbegin(); it != entries_.rend(); ++it)
    if (it->committed) return it->id;
  return {};
}
Status RealtimeTranscriptHistory::Transcript(
    const RealtimeTranscriptTicket& ticket,
    std::optional<std::string_view>* output) const {
  const auto index = Position(ticket);
  if (!output || index == entries_.size() || !entries_[index].committed)
    return Invalid();
  *output = entries_[index].state == State::kCompleted
                ? std::optional<std::string_view>(entries_[index].text)
                : std::nullopt;
  return {};
}
Status RealtimeTranscriptHistory::Commit(
    const RealtimeTranscriptTicket& ticket) {
  const auto index = Position(ticket);
  if (index == entries_.size() || entries_[index].committed ||
      entries_[index].state != State::kPending)
    return Invalid();
  entries_[index].committed = true;
  return {};
}
Status RealtimeTranscriptHistory::Complete(
    const RealtimeTranscriptTicket& ticket, std::string_view transcript) {
  const auto index = Position(ticket);
  if (index == entries_.size()) return Invalid();
  auto& entry = entries_[index];
  if (!entry.committed || entry.state != State::kPending ||
      transcript.size() > entry.charge ||
      transcript.find('\0') != transcript.npos ||
      detail::NormalizeUtf8(transcript) != transcript)
    return Invalid();
  std::string text(transcript);
  entry.text.swap(text);
  charged_ -= entry.charge - transcript.size();
  entry.charge = transcript.size();
  entry.state = State::kCompleted;
  return {};
}
Status RealtimeTranscriptHistory::Fail(const RealtimeTranscriptTicket& ticket) {
  const auto index = Position(ticket);
  if (index == entries_.size() || entries_[index].state != State::kPending)
    return Invalid();
  if (!entries_[index].committed) return Erase(ticket);
  auto& entry = entries_[index];
  charged_ -= entry.charge;
  entry.charge = 0;
  entry.state = State::kFailed;
  return {};
}
Status RealtimeTranscriptHistory::Erase(
    const RealtimeTranscriptTicket& ticket) {
  const auto index = Position(ticket);
  if (index == entries_.size()) return Invalid();
  charged_ -= entries_[index].charge;
  entries_.erase(entries_.begin() + index);
  return {};
}
Status RealtimeTranscriptHistory::ContextBefore(
    const RealtimeTranscriptTicket& ticket, size_t maximum_bytes,
    std::string* output) const {
  const auto end = Position(ticket);
  if (!output || end == entries_.size() || maximum_bytes > maximum_bytes_)
    return Invalid();
  size_t bytes = 0, nonempty = 0;
  for (size_t i = 0; i < end; ++i) {
    const auto& entry = entries_[i];
    if (!entry.committed) continue;
    if (entry.state == State::kPending)
      return Status::Fail("Earlier committed transcription is still pending");
    if (entry.state != State::kCompleted || entry.text.empty()) continue;
    const auto needed = entry.text.size() + size_t(nonempty != 0);
    if (needed > maximum_bytes - bytes)
      return Status::Fail("Earlier transcript context exceeds its byte budget");
    bytes += needed;
    ++nonempty;
  }
  std::string context;
  context.reserve(bytes);
  for (size_t i = 0; i < end; ++i) {
    const auto& entry = entries_[i];
    if (!entry.committed || entry.state != State::kCompleted ||
        entry.text.empty())
      continue;
    if (!context.empty()) context += '\n';
    context += entry.text;
  }
  *output = std::move(context);
  return {};
}
}  // namespace q4t::server
