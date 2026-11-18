// A producer owns observations only until its step is explicitly finished.
#pragma once

#include "q4t/server/actual_usage.h"

namespace q4t::server {
class ActualUsageStep {
 public:
  explicit ActualUsageStep(ActualUsageCounter* counter, bool input = true,
                           bool include = true) noexcept
      : counter_(counter) {
    if (counter_) counter_->BeginStep(input, include);
  }
  ~ActualUsageStep() { Finish(); }
  ActualUsageStep(const ActualUsageStep&) = delete;
  ActualUsageStep& operator=(const ActualUsageStep&) = delete;

  bool Active() const noexcept { return counter_ != nullptr; }
  void Observe(const ActualUsage& value) noexcept {
    if (counter_) counter_->Observe(value);
  }
  void Finish() noexcept {
    if (!counter_) return;
    counter_->EndStep();
    counter_ = nullptr;
  }

 private:
  ActualUsageCounter* counter_ = nullptr;
};
}  // namespace q4t::server
