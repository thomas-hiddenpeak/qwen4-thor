#include "q4t/server/realtime_speech_history.h"

#include <algorithm>

#include "q4t/server/input_contract.h"
#include "q4t/server/lifetime.h"
#include "q4t/server/realtime_speech_encoder.h"

namespace q4t::server {
namespace {
Status Invalid(std::string_view reason) {
  return Status::Fail("Realtime audio history: " + std::string(reason));
}
size_t Width(RealtimeAudioFormat format) {
  return format.encoding == RealtimeAudioEncoding::kPcm16 ? 2 : 1;
}
bool FormatValid(RealtimeAudioFormat format) {
  return format.encoding == RealtimeAudioEncoding::kPcm16
             ? format.rate == 24000
             : (format.encoding == RealtimeAudioEncoding::kMuLaw ||
                format.encoding == RealtimeAudioEncoding::kALaw) &&
                   format.rate == 8000;
}
}  // namespace

RealtimeSpeechHistory::RealtimeSpeechHistory(std::string owner,
                                             size_t maximum_bytes,
                                             size_t maximum_items)
    : owner_(std::move(owner)),
      maximum_bytes_(maximum_bytes),
      maximum_items_(maximum_items) {
  valid_ = ValidRequestId(owner_) && maximum_bytes &&
           maximum_bytes <= kMaximumBytes && maximum_items &&
           maximum_items <= RealtimeConversation::kMaximumItems;
}

size_t RealtimeSpeechHistory::Position(const RealtimeItemTicket& ticket) const {
  if (valid_ && ticket.owner == owner_ && ticket.generation &&
      ValidRequestId(ticket.id))
    for (size_t i = 0; i < entries_.size(); ++i)
      if (entries_[i].ticket == ticket) return i;
  return entries_.size();
}

Status RealtimeSpeechHistory::Begin(const RealtimeItemTicket& ticket,
                                    RealtimeAudioFormat format,
                                    size_t maximum_record_bytes) {
  if (!valid_ || ticket.owner != owner_ || !ticket.generation ||
      !ValidRequestId(ticket.id) || !FormatValid(format) ||
      entries_.size() == maximum_items_ || !maximum_record_bytes ||
      maximum_record_bytes > kRealtimeAudioBytes ||
      maximum_record_bytes % Width(format) ||
      maximum_record_bytes > maximum_bytes_ - charged_)
    return Invalid("invalid owner, format or reservation");
  if (std::ranges::any_of(entries_, [&](const auto& entry) {
        return entry.ticket.id == ticket.id;
      }))
    return Invalid("item already owns audio");
  Entry entry;
  entry.ticket = ticket;
  entry.format = format;
  entry.maximum = maximum_record_bytes;
  entry.bytes.reserve(maximum_record_bytes);
  const auto charge = entry.bytes.capacity();
  if (charge > maximum_bytes_ - charged_)
    return Invalid("allocated audio capacity exceeds reservation");
  entries_.push_back(std::move(entry));
  charged_ += charge;
  return {};
}

Status RealtimeSpeechHistory::Append(const RealtimeItemTicket& ticket,
                                     std::string_view bytes) {
  const auto position = Position(ticket);
  if (position == entries_.size()) return Invalid("stale audio ticket");
  auto& entry = entries_[position];
  if (entry.complete || entry.truncated || entry.interrupted || bytes.empty() ||
      bytes.size() > RealtimeSpeechEncoder::kMaximumChunkBytes ||
      bytes.size() % Width(entry.format) ||
      bytes.size() > entry.maximum - entry.bytes.size())
    return Invalid("invalid audio state or chunk extent");
  // Capacity was charged at Begin. Append cannot allocate or grow the quota.
  entry.bytes.append(bytes);
  entry.generated_samples = entry.bytes.size() / Width(entry.format);
  return {};
}

void RealtimeSpeechHistory::Compact(Entry& entry) noexcept {
  // One bounded transient copy is covered by the session reservation. Failure
  // keeps the existing charge and valid audio; it cannot fail healthy output.
  if (entry.bytes.size() >= entry.bytes.capacity() / 2) return;
  try {
    std::string compact(entry.bytes);
    if (compact.capacity() < entry.bytes.capacity()) {
      charged_ -= entry.bytes.capacity() - compact.capacity();
      entry.bytes.swap(compact);
    }
  } catch (...) {
  }
}

Status RealtimeSpeechHistory::Complete(const RealtimeItemTicket& ticket) {
  const auto position = Position(ticket);
  if (position == entries_.size()) return Invalid("stale audio ticket");
  auto& entry = entries_[position];
  if (entry.complete || entry.truncated || entry.interrupted ||
      entry.bytes.empty())
    return Invalid("audio cannot complete in its current state");
  entry.complete = true;
  Compact(entry);
  return {};
}

Status RealtimeSpeechHistory::Interrupt(const RealtimeItemTicket& ticket) {
  const auto position = Position(ticket);
  if (position == entries_.size()) return Invalid("stale audio ticket");
  auto& entry = entries_[position];
  // An earlier client truncate or a verified EOF remains the stronger fact.
  if (!entry.complete) {
    if (!entry.truncated) entry.interrupted = true;
    Compact(entry);
  }
  return {};
}

Status RealtimeSpeechHistory::Truncate(const RealtimeItemTicket& ticket,
                                       uint64_t audio_end_ms) {
  const auto position = Position(ticket);
  if (position == entries_.size()) return Invalid("stale audio ticket");
  auto& entry = entries_[position];
  const uint64_t samples = entry.bytes.size() / Width(entry.format);
  // Compare before multiplication to reject oversized/overflowing requests.
  if (audio_end_ms > samples * 1000 / entry.format.rate)
    return Invalid("played duration exceeds available audio");
  const uint64_t retained = audio_end_ms * entry.format.rate / 1000;
  entry.bytes.resize(retained * Width(entry.format));
  entry.truncated = true;
  entry.complete = false;
  return {};
}

Status RealtimeSpeechHistory::Get(const RealtimeItemTicket& ticket,
                                  RealtimeSpeechView* view) const {
  const auto position = Position(ticket);
  if (!view || position == entries_.size()) return Invalid("stale audio view");
  const auto& entry = entries_[position];
  *view = {entry.format,   entry.bytes,     entry.generated_samples,
           entry.complete, entry.truncated, entry.interrupted};
  return {};
}

Status RealtimeSpeechHistory::Erase(const RealtimeItemTicket& ticket) {
  const auto position = Position(ticket);
  if (position == entries_.size()) return Invalid("stale audio ticket");
  const auto charge = entries_[position].bytes.capacity();
  entries_.erase(entries_.begin() + position);
  charged_ -= charge;
  return {};
}
}  // namespace q4t::server
