// Request/scheduler handoff for progress; does not submit or synchronize work.
#pragma once

#include <span>

#include "q4t/server/request_progress.h"

namespace q4t::server {

class ProgressContext {
 public:
  explicit ProgressContext(ProgressHandle handle)
      : single_(handle), previous_(current_) {
    current_ = {&single_, 1};
  }
  explicit ProgressContext(std::span<const ProgressHandle> handles)
      : previous_(current_) {
    current_ = handles;
  }
  ~ProgressContext() { current_ = previous_; }
  ProgressContext(const ProgressContext&) = delete;
  ProgressContext& operator=(const ProgressContext&) = delete;
  static std::span<const ProgressHandle> Current() { return current_; }

 private:
  ProgressHandle single_;
  std::span<const ProgressHandle> previous_;
  static inline thread_local std::span<const ProgressHandle> current_;
};

// Scheduler storage is bounded independently of an incoming batch's size.
class ProgressBatch {
 public:
  template <typename Rows, typename Handle>
  ProgressBatch(const Rows& rows, Handle handle) {
    for (const auto& row : rows) {
      if (size_ == handles_.size()) break;
      handles_[size_++] = handle(row);
    }
  }
  std::span<const ProgressHandle> Handles() const {
    return {handles_.data(), size_};
  }

 private:
  std::array<ProgressHandle, RequestProgress::kCapacity> handles_{};
  size_t size_ = 0;
};

class ProgressExecution {
 public:
  explicit ProgressExecution(ProgressWork work)
      : previous_(active_), handles_(ProgressContext::Current()) {
    // Nested helper calls belong to the same outer operation. Only actual
    // completion boundaries pulse it; helper scope exit cannot fake progress.
    if (previous_) {
      isolated_ = handles_.data() != previous_->handles_.data() ||
                  handles_.size() != previous_->handles_.size();
      if (isolated_) {
        for (const auto& handle : handles_) handle.Begin(ProgressWork::kNone);
        active_ = this;
      }
      return;
    }
    active_ = this;
    for (const auto& handle : handles_) handle.Begin(work);
  }
  ~ProgressExecution() {
    if (previous_) {
      if (isolated_) active_ = previous_;
      return;
    }
    for (const auto& handle : handles_) handle.End();
    active_ = nullptr;
  }
  ProgressExecution(const ProgressExecution&) = delete;
  ProgressExecution& operator=(const ProgressExecution&) = delete;
  static void Completed() noexcept {
    if (active_ && !active_->isolated_)
      for (const auto& handle : active_->handles_) handle.Completed();
  }

 private:
  bool isolated_ = false;
  ProgressExecution* previous_;
  std::span<const ProgressHandle> handles_;
  static inline thread_local ProgressExecution* active_ = nullptr;
};

}  // namespace q4t::server
