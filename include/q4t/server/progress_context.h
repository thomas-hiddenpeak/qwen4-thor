// Request/scheduler handoff for progress; does not submit or synchronize work.
#pragma once

#include <span>

#include "q4t/runtime/execution_progress.h"
#include "q4t/server/request_progress.h"

namespace q4t::server {

class ProgressContext {
 public:
  explicit ProgressContext(ProgressHandle handle)
      : single_(handle), previous_(current_) {
    current_ = {&single_, 1};
    InstallObserver();
  }
  explicit ProgressContext(std::span<const ProgressHandle> handles)
      : previous_(current_) {
    current_ = handles;
    InstallObserver();
  }
  ~ProgressContext() {
    runtime::execution_progress_observer = previous_observer_;
    current_ = previous_;
  }
  ProgressContext(const ProgressContext&) = delete;
  ProgressContext& operator=(const ProgressContext&) = delete;
  static std::span<const ProgressHandle> Current() { return current_; }

 private:
  void InstallObserver() noexcept {
    previous_observer_ = runtime::execution_progress_observer;
    runtime::execution_progress_observer = {this, Activity};
    handles_ = current_;
  }
  static void Activity(void* context, runtime::ExecutionActivityKind kind,
                       runtime::ExecutionProgressEvent event) noexcept;
  ProgressHandle single_;
  std::span<const ProgressHandle> previous_;
  std::span<const ProgressHandle> handles_;
  runtime::ExecutionProgressObserver previous_observer_;
  std::array<ProgressStage, RequestProgress::kCapacity> saved_stages_{};
  std::array<runtime::ExecutionActivityKind, 8> activities_{};
  size_t depth_ = 0;
  bool invalid_ = false;
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

inline void ProgressContext::Activity(
    void* context, runtime::ExecutionActivityKind kind,
    runtime::ExecutionProgressEvent event) noexcept {
  auto& owner = *static_cast<ProgressContext*>(context);
  using Kind = runtime::ExecutionActivityKind;
  using Event = runtime::ExecutionProgressEvent;
  const auto unknown = [&] {
    owner.invalid_ = true;
    for (const auto& handle : owner.handles_) handle.Begin(ProgressWork::kNone);
  };
  if (owner.invalid_) return;
  if (owner.handles_.size() > owner.saved_stages_.size()) {
    unknown();
    return;
  }
  const auto stage = [](Kind activity) {
    switch (activity) {
      case Kind::kPleRead: return ProgressStage::kPleRead;
      case Kind::kPleUploadWait: return ProgressStage::kPleUploadWait;
      case Kind::kMediaDecode: return ProgressStage::kMediaDecode;
      case Kind::kMediaResize: return ProgressStage::kMediaResize;
      case Kind::kMediaNormalize: return ProgressStage::kMediaNormalize;
      case Kind::kMediaPatchify: return ProgressStage::kMediaPatchify;
      case Kind::kMediaPack: return ProgressStage::kMediaPack;
      case Kind::kMediaUpload: return ProgressStage::kMediaUpload;
      case Kind::kMediaWorkspace: return ProgressStage::kMediaWorkspace;
      case Kind::kMediaForward: return ProgressStage::kMediaForward;
      case Kind::kCleanup: return ProgressStage::kCleanup;
    }
    return ProgressStage::kInput;
  };
  if (event == Event::kBegin) {
    if (owner.depth_ == owner.activities_.size()) {
      unknown();
      return;
    }
    if (owner.depth_ == 0)
      for (size_t i = 0; i < owner.handles_.size(); ++i)
        owner.saved_stages_[i] = owner.handles_[i].StageValue();
    owner.activities_[owner.depth_++] = kind;
    for (const auto& handle : owner.handles_) handle.Stage(stage(kind));
    return;
  }
  if (!owner.depth_ || owner.activities_[owner.depth_ - 1] != kind) {
    unknown();
    return;
  }
  if (event == Event::kCompleted) {
    // A callback captured in another context cannot count the current work.
    if (ProgressContext::Current().data() != owner.handles_.data()) {
      unknown();
      return;
    }
    ProgressExecution::Completed();
    return;
  }
  --owner.depth_;
  for (size_t i = 0; i < owner.handles_.size(); ++i)
    owner.handles_[i].Stage(owner.depth_
        ? stage(owner.activities_[owner.depth_ - 1]) : owner.saved_stages_[i]);
}

}  // namespace q4t::server
