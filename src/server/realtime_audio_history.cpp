#include "q4t/server/realtime_audio_history.h"

#include <algorithm>

#include "q4t/server/input_contract.h"

namespace q4t::server {
namespace {
Status Invalid() {
  return Status::Fail("Realtime audio history identity or extent invalid");
}
Status Full() {
  return Status::Fail("Delete prior items to release audio history capacity");
}
}  // namespace

std::span<const float> RealtimeAudioRecord::Samples() const {
  return stream_ ? stream_->SealedSamples(epoch_) : std::span(pcm_);
}
std::shared_ptr<AsrStreamInput> RealtimeAudioRecord::Stream() {
  return stream_ ? std::shared_ptr<AsrStreamInput>(shared_from_this(),
                                                   stream_.get())
                 : nullptr;
}
RealtimeAudioHistory::RealtimeAudioHistory(std::string owner, size_t bytes,
                                           size_t items)
    : owner_(std::move(owner)),
      maximum_items_(items),
      budget_(std::make_shared<HttpBodyBudget>(bytes)) {
  valid_ = ValidRequestId(owner_) && bytes &&
           bytes <= kRealtimeAudioHistoryBytes && items && items <= 128;
  if (valid_) entries_.reserve(items);
}
bool RealtimeAudioHistory::CanAdd(
    const RealtimeTranscriptTicket& ticket) const {
  return valid_ && ticket.owner == owner_ && ticket.generation &&
         ValidRequestId(ticket.id) && ticket.id != "root" &&
         entries_.size() < maximum_items_ &&
         std::ranges::none_of(entries_, [&](const auto& entry) {
           return entry.ticket.id == ticket.id;
         });
}
Status RealtimeAudioHistory::Decode(
    const RealtimeTranscriptTicket& ticket, const RealtimeAudioItem& item,
    const std::array<std::span<const uint8_t>, 2>& bytes, size_t max_samples,
    std::shared_ptr<RealtimeAudioRecord>* output,
    const std::function<bool()>& cancelled) {
  if (!output || !CanAdd(ticket) || !item.format.rate ||
      item.samples > kRealtimeAudioBytes || !max_samples ||
      max_samples > 600ULL * 16000)
    return Invalid();
  const size_t samples = uint64_t(item.samples) * 16000 / item.format.rate;
  if (!samples || samples > max_samples) return Invalid();
  auto record =
      std::shared_ptr<RealtimeAudioRecord>(new RealtimeAudioRecord(budget_));
  if (!record->lease_.Acquire(samples * sizeof(float))) return Full();
  const auto status =
      DecodeRealtimePcm(item, bytes, max_samples, &record->pcm_, cancelled);
  if (!status) return status;
  // Charge allocation capacity, including a library's possible over-allocation.
  if (!record->lease_.GrowTo(record->pcm_.capacity() * sizeof(float)))
    return Full();
  entries_.push_back({ticket, record});
  *output = std::move(record);
  return {};
}
Status RealtimeAudioHistory::BeginStream(
    const RealtimeTranscriptTicket& ticket, uint64_t epoch,
    size_t maximum_samples, size_t chunk_samples,
    std::shared_ptr<RealtimeAudioRecord>* output) {
  if (!output || !CanAdd(ticket) || !epoch || !maximum_samples ||
      maximum_samples > 600ULL * 16000)
    return Invalid();
  auto record =
      std::shared_ptr<RealtimeAudioRecord>(new RealtimeAudioRecord(budget_));
  if (!record->lease_.Acquire(maximum_samples * sizeof(float))) return Full();
  record->stream_ =
      std::make_unique<AsrStreamInput>(epoch, maximum_samples, chunk_samples);
  if (!record->stream_->Valid()) return Invalid();
  record->epoch_ = epoch;
  entries_.push_back({ticket, record});
  *output = std::move(record);
  return {};
}
std::shared_ptr<RealtimeAudioRecord> RealtimeAudioHistory::Find(
    const RealtimeTranscriptTicket& ticket) const {
  if (valid_)
    for (const auto& entry : entries_)
      if (entry.ticket == ticket) return entry.audio;
  return {};
}
Status RealtimeAudioHistory::Erase(const RealtimeTranscriptTicket& ticket) {
  if (valid_)
    for (auto it = entries_.begin(); it != entries_.end(); ++it)
      if (it->ticket == ticket) {
        entries_.erase(it);
        return {};
      }
  return Invalid();
}
}  // namespace q4t::server
