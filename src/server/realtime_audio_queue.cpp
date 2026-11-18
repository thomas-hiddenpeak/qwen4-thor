#include "q4t/server/realtime_audio_queue.h"

#include <algorithm>
#include <cstring>
#include <limits>

namespace q4t::server {
namespace {
bool RateValid(uint32_t rate) {
  return rate == 8000 || rate == 16000 || rate == 24000 || rate == 48000;
}
}  // namespace

Status ProjectRealtimeSamples(uint64_t ticks, uint32_t rate,
                              RealtimeSamplePosition* output) {
  if (!output || !RateValid(rate))
    return Status::Fail("invalid realtime sample clock rate");
  const auto step = kRealtimeTicksPerSecond / rate;
  *output = {ticks / step, static_cast<uint32_t>(ticks % step)};
  return {};
}
bool RealtimeAudioQueue::FormatValid(RealtimeAudioFormat format) {
  if (format.encoding == RealtimeAudioEncoding::kPcm16)
    return RateValid(format.rate);
  return (format.encoding == RealtimeAudioEncoding::kMuLaw ||
          format.encoding == RealtimeAudioEncoding::kALaw) &&
         format.rate == 8000;
}
size_t RealtimeAudioQueue::SampleBytes(RealtimeAudioFormat format) {
  return format.encoding == RealtimeAudioEncoding::kPcm16 ? 2 : 1;
}
RealtimeAudioQueue::RealtimeAudioQueue(uint64_t epoch,
                                       RealtimeAudioFormat format,
                                       size_t capacity_bytes, size_t item_limit,
                                       uint64_t duration_ticks)
    : epoch_(epoch),
      capacity_bytes_(capacity_bytes),
      item_limit_(item_limit),
      duration_ticks_(duration_ticks),
      format_(format) {
  valid_ = epoch && FormatValid(format) &&
           capacity_bytes >= SampleBytes(format) &&
           capacity_bytes <= kRealtimeAudioBytes && item_limit &&
           item_limit <= kRealtimeQueuedItems && duration_ticks &&
           duration_ticks <= kRealtimeSessionTicks;
}
Status RealtimeAudioQueue::Append(uint64_t epoch,
                                  std::span<const uint8_t> bytes) {
  if (!Owns(epoch) || sealed_)
    return Status::Fail("realtime input ownership or lifecycle differs");
  if (bytes.size() > capacity_bytes_ - used_bytes_)
    return Status::Fail("realtime audio byte capacity exhausted");
  const auto width = SampleBytes(format_);
  const auto samples =
      (pending_bytes_ + bytes.size()) / width - pending_bytes_ / width;
  const auto step = kRealtimeTicksPerSecond / format_.rate;
  if (samples > (duration_ticks_ - ticks_) / step)
    return Status::Fail("realtime session audio duration exhausted");
  if (bytes.empty()) return {};
  if (!storage_) storage_ = std::make_unique<uint8_t[]>(capacity_bytes_);
  const auto tail = (head_ + used_bytes_) % capacity_bytes_;
  const auto first = std::min(bytes.size(), capacity_bytes_ - tail);
  std::memcpy(storage_.get() + tail, bytes.data(), first);
  if (first < bytes.size())
    std::memcpy(storage_.get(), bytes.data() + first, bytes.size() - first);
  used_bytes_ += bytes.size();
  pending_bytes_ += bytes.size();
  ticks_ += samples * step;
  return {};
}
Status RealtimeAudioQueue::Commit(uint64_t epoch, RealtimeAudioItem* item) {
  if (!Owns(epoch) || sealed_ || !item || !pending_bytes_ ||
      pending_bytes_ % SampleBytes(format_))
    return Status::Fail(
        "realtime commit requires complete uncommitted samples");
  if (item_count_ == item_limit_ ||
      sequence_ == std::numeric_limits<uint64_t>::max())
    return Status::Fail("realtime committed item capacity exhausted");
  const RealtimeAudioItem value{epoch_,
                                sequence_ + 1,
                                pending_begin_,
                                ticks_,
                                pending_bytes_ / SampleBytes(format_),
                                pending_bytes_,
                                format_};
  items_[(item_head_ + item_count_) % kRealtimeQueuedItems] = value;
  ++item_count_;
  ++sequence_;
  pending_bytes_ = 0;
  pending_begin_ = ticks_;
  *item = value;
  return {};
}
Status RealtimeAudioQueue::Clear(uint64_t epoch) {
  if (!Owns(epoch) || sealed_)
    return Status::Fail("realtime input ownership or lifecycle differs");
  used_bytes_ -= pending_bytes_;
  pending_bytes_ = 0;
  pending_begin_ = ticks_;
  return {};
}
Status RealtimeAudioQueue::ChangeFormat(uint64_t epoch,
                                        RealtimeAudioFormat format) {
  if (!Owns(epoch) || sealed_ || used_bytes_ || !FormatValid(format) ||
      capacity_bytes_ < SampleBytes(format))
    return Status::Fail("realtime format change requires drained audio");
  format_ = format;
  return {};
}
const RealtimeAudioItem* RealtimeAudioQueue::Front() const {
  return item_count_ ? &items_[item_head_] : nullptr;
}
std::array<std::span<const uint8_t>, 2> RealtimeAudioQueue::FrontBytes() const {
  if (!item_count_) return {};
  const auto bytes = items_[item_head_].bytes;
  const auto first = std::min(bytes, capacity_bytes_ - head_);
  return {{{storage_.get() + head_, first}, {storage_.get(), bytes - first}}};
}
Status RealtimeAudioQueue::Acknowledge(uint64_t epoch, uint64_t sequence) {
  if (!Owns(epoch) || !item_count_ || items_[item_head_].sequence != sequence)
    return Status::Fail("realtime audio completion ownership differs");
  const auto bytes = items_[item_head_].bytes;
  head_ = (head_ + bytes) % capacity_bytes_;
  used_bytes_ -= bytes;
  items_[item_head_] = {};
  item_head_ = (item_head_ + 1) % kRealtimeQueuedItems;
  --item_count_;
  return {};
}
Status RealtimeAudioQueue::FinishInput(uint64_t epoch) {
  if (!Owns(epoch) || sealed_ || pending_bytes_)
    return Status::Fail("realtime drain requires resolved input tail");
  sealed_ = true;
  return {};
}
Status RealtimeAudioQueue::Cancel(uint64_t epoch) {
  if (!Owns(epoch))
    return Status::Fail("realtime cancellation ownership differs");
  used_bytes_ -= pending_bytes_;
  pending_bytes_ = 0;
  pending_begin_ = ticks_;
  sealed_ = cancelled_ = true;
  return {};
}
}  // namespace q4t::server
