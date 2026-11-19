// Fixed-capacity cumulative audio shared by one producer and one ASR worker.
#pragma once

#include <chrono>
#include <condition_variable>
#include <functional>
#include <memory>
#include <mutex>

#include "q4t/server/asr_stream_state.h"

namespace q4t::server {
enum class AsrStreamReadKind { kReady, kEnd, kCancelled, kDeadline, kInvalid };
struct AsrStreamRead {
  AsrStreamReadKind kind = AsrStreamReadKind::kInvalid;
  AsrStreamStep step;
  std::span<const float> samples;
};

// The producer writes only beyond published samples. A consumer borrows an
// immutable cumulative prefix until Complete, including after cancellation.
// Storage never reallocates/overwrites. The session reserves StorageBytes and
// retains this owner until its worker joins; Cancel is not deallocation.
class AsrStreamInput {
 public:
  using Clock = std::chrono::steady_clock;
  AsrStreamInput(uint64_t epoch, size_t maximum_samples, size_t chunk_samples,
                 size_t maximum_steps = 4096);
  bool Valid() const { return bool(storage_); }
  size_t StorageBytes() const {
    return storage_ ? maximum_ * sizeof(float) : 0;
  }
  Status Append(uint64_t epoch, std::span<const float> samples);
  Status Seal(uint64_t epoch);
  Status Cancel(uint64_t epoch);
  // No model/sequence lock is held while waiting. A real absolute request
  // deadline and cooperative cancellation bound the wait; arrivals do not
  // extend it or fabricate execution progress.
  AsrStreamRead Read(Clock::time_point deadline,
                     const std::function<bool()>& cancelled = {});
  Status Complete(const AsrStreamStep& step);
  size_t Received() const;
  bool Drained() const;
  // Available only after an actual successful Seal. The caller retains this
  // owner; later cancellation cannot invalidate/overwrite the sealed samples.
  std::span<const float> SealedSamples(uint64_t epoch) const;

 private:
  const uint64_t epoch_;
  const size_t maximum_;
  mutable std::mutex mutex_;
  std::condition_variable changed_;
  AsrStreamSchedule schedule_;
  std::unique_ptr<float[]> storage_;
  bool sealed_ = false;
};
}  // namespace q4t::server
