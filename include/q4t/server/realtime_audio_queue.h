// Request-owned realtime input storage and exact session sample positions.
#pragma once

#include <array>
#include <cstddef>
#include <cstdint>
#include <memory>
#include <span>

#include "q4t/status.h"

namespace q4t::server {
inline constexpr uint64_t kRealtimeTicksPerSecond = 48000;
inline constexpr uint64_t kRealtimeSessionTicks =
    3600 * kRealtimeTicksPerSecond;
inline constexpr size_t kRealtimeAudioBytes = 16ULL << 20;
inline constexpr size_t kRealtimeQueuedItems = 32;

enum class RealtimeAudioEncoding { kPcm16, kMuLaw, kALaw };
struct RealtimeAudioFormat {
  RealtimeAudioEncoding encoding = RealtimeAudioEncoding::kPcm16;
  uint32_t rate = 24000;
  bool operator==(const RealtimeAudioFormat&) const = default;
};
struct RealtimeSamplePosition {
  uint64_t sample = 0;
  // Exact residual in 48kHz ticks; preserve this across input chunks. This is
  // a time mapping only, not a substitute for a phase-preserving resampler.
  uint32_t remainder_ticks = 0;
  bool operator==(const RealtimeSamplePosition&) const = default;
};
Status ProjectRealtimeSamples(uint64_t ticks, uint32_t rate,
                              RealtimeSamplePosition* output);

struct RealtimeAudioItem {
  uint64_t epoch = 0, sequence = 0;
  uint64_t begin_ticks = 0, end_ticks = 0;
  size_t samples = 0, bytes = 0;
  RealtimeAudioFormat format;
  bool operator==(const RealtimeAudioItem&) const = default;
};

// One producer/event loop owns mutations. Async consumers borrow the front
// item's immutable byte spans until their exact epoch/sequence is acknowledged.
// Cancel does not free committed storage out from underneath a running worker.
// The owner must reserve capacity_bytes plus sizeof(queue) before construction,
// and retain the queue until all borrowers stop; no thread or implicit restart.
class RealtimeAudioQueue {
 public:
  explicit RealtimeAudioQueue(uint64_t epoch, RealtimeAudioFormat format = {},
                              size_t capacity_bytes = kRealtimeAudioBytes,
                              size_t item_limit = kRealtimeQueuedItems,
                              uint64_t duration_ticks = kRealtimeSessionTicks);
  bool Valid() const { return valid_; }
  Status Append(uint64_t epoch, std::span<const uint8_t> bytes);
  Status Commit(uint64_t epoch, RealtimeAudioItem* item);
  // Clear only the uncommitted tail; accepted sample time is never refunded.
  Status Clear(uint64_t epoch);
  // Changing format requires all prior audio to have been acknowledged. The
  // absolute clock survives the change; protocol-level format support belongs
  // to the session adapter, not to this storage class.
  Status ChangeFormat(uint64_t epoch, RealtimeAudioFormat format);
  const RealtimeAudioItem* Front() const;
  std::array<std::span<const uint8_t>, 2> FrontBytes() const;
  Status Acknowledge(uint64_t epoch, uint64_t sequence);
  // Normal drain requires the caller to explicitly commit/clear the tail first.
  Status FinishInput(uint64_t epoch);
  Status Cancel(uint64_t epoch);
  bool Cancelled() const { return cancelled_; }
  bool Drained() const { return sealed_ && used_bytes_ == 0; }
  size_t PendingBytes() const { return pending_bytes_; }
  size_t UsedBytes() const { return used_bytes_; }
  size_t Items() const { return item_count_; }
  size_t StorageBytes() const { return storage_ ? capacity_bytes_ : 0; }
  uint64_t AcceptedTicks() const { return ticks_; }

 private:
  bool Owns(uint64_t epoch) const { return valid_ && epoch == epoch_; }
  static bool FormatValid(RealtimeAudioFormat format);
  static size_t SampleBytes(RealtimeAudioFormat format);
  const uint64_t epoch_;
  const size_t capacity_bytes_, item_limit_;
  const uint64_t duration_ticks_;
  RealtimeAudioFormat format_;
  std::unique_ptr<uint8_t[]> storage_;
  std::array<RealtimeAudioItem, kRealtimeQueuedItems> items_{};
  size_t head_ = 0, used_bytes_ = 0, pending_bytes_ = 0;
  size_t item_head_ = 0, item_count_ = 0;
  uint64_t ticks_ = 0, pending_begin_ = 0, sequence_ = 0;
  bool valid_ = false, sealed_ = false, cancelled_ = false;
};
}  // namespace q4t::server
