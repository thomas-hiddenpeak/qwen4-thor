#include "q4t/server/asr_stream_input.h"

#include <algorithm>
#include <cmath>

namespace q4t::server {
AsrStreamInput::AsrStreamInput(uint64_t epoch, size_t maximum_samples,
                               size_t chunk_samples, size_t maximum_steps)
    : epoch_(epoch),
      maximum_(maximum_samples),
      schedule_(epoch, chunk_samples, maximum_samples, maximum_steps) {
  if (schedule_.Valid()) storage_ = std::make_unique<float[]>(maximum_);
}
Status AsrStreamInput::Append(uint64_t epoch, std::span<const float> samples) {
  const std::lock_guard lock(mutex_);
  const auto received = schedule_.Received();
  if (!storage_ || samples.size() > maximum_ - received ||
      !std::ranges::all_of(samples, [](float x) { return std::isfinite(x); }))
    return Status::Fail("Streaming audio input is invalid or full");
  const auto status = schedule_.Append(epoch, samples.size());
  if (!status) return status;
  std::copy(samples.begin(), samples.end(), storage_.get() + received);
  changed_.notify_one();
  return {};
}
Status AsrStreamInput::Seal(uint64_t epoch) {
  const std::lock_guard lock(mutex_);
  const auto status = schedule_.Seal(epoch);
  if (status) sealed_ = true;
  changed_.notify_one();
  return status;
}
Status AsrStreamInput::Cancel(uint64_t epoch) {
  const std::lock_guard lock(mutex_);
  const auto status = schedule_.Cancel(epoch);
  changed_.notify_one();
  return status;
}
AsrStreamRead AsrStreamInput::Read(Clock::time_point deadline,
                                   const std::function<bool()>& cancelled,
                                   bool latest_prefix) {
  for (;;) {
    if (cancelled && cancelled())
      return {AsrStreamReadKind::kCancelled, {}, {}};
    std::unique_lock lock(mutex_);
    const auto now = Clock::now();
    if (!storage_ || schedule_.InFlight()) return {};
    if (schedule_.Cancelled()) return {AsrStreamReadKind::kCancelled, {}, {}};
    if (now >= deadline) return {AsrStreamReadKind::kDeadline, {}, {}};
    if (schedule_.Drained()) return {AsrStreamReadKind::kEnd, {}, {}};
    if (schedule_.Ready()) {
      AsrStreamStep step;
      // Epoch is fixed by the owned scheduler; no data from a previous item
      // can be installed into this object's storage.
      if (!schedule_.Next(epoch_, &step, latest_prefix)) return {};
      return {AsrStreamReadKind::kReady,
              step,
              {storage_.get(), size_t(step.samples)}};
    }
    changed_.wait_until(
        lock, std::min(deadline, now + std::chrono::milliseconds(50)));
  }
}
Status AsrStreamInput::Complete(const AsrStreamStep& step) {
  const std::lock_guard lock(mutex_);
  const auto status = schedule_.Complete(step);
  changed_.notify_one();
  return status;
}
size_t AsrStreamInput::Received() const {
  const std::lock_guard lock(mutex_);
  return schedule_.Received();
}
bool AsrStreamInput::Drained() const {
  const std::lock_guard lock(mutex_);
  return schedule_.Drained();
}
std::span<const float> AsrStreamInput::SealedSamples(uint64_t epoch) const {
  const std::lock_guard lock(mutex_);
  return epoch == epoch_ && sealed_
             ? std::span<const float>(storage_.get(), schedule_.Received())
             : std::span<const float>{};
}
}  // namespace q4t::server
