// Optional, allocation-free observation of synchronous completion boundaries.
// Runtime/PLE/vision do not depend on a server, logger, or monitoring process.
#pragma once

namespace q4t::runtime {

enum class ExecutionActivityKind {
  kPleRead, kPleUploadWait, kMediaDecode, kMediaResize, kMediaNormalize,
  kMediaPatchify, kMediaPack, kMediaUpload, kMediaWorkspace, kMediaForward,
  kCleanup
};
enum class ExecutionProgressEvent { kBegin, kCompleted, kEnd };
struct ExecutionProgressObserver {
  void* context = nullptr;
  void (*notify)(void*, ExecutionActivityKind,
                 ExecutionProgressEvent) noexcept = nullptr;
};
inline thread_local ExecutionProgressObserver execution_progress_observer;

class ExecutionActivity {
 public:
  explicit ExecutionActivity(ExecutionActivityKind kind) noexcept
      : kind_(kind), observer_(execution_progress_observer) {
    Notify(ExecutionProgressEvent::kBegin);
  }
  ~ExecutionActivity() { End(); }
  ExecutionActivity(const ExecutionActivity&) = delete;
  ExecutionActivity& operator=(const ExecutionActivity&) = delete;
  void Completed() noexcept { Notify(ExecutionProgressEvent::kCompleted); }
  void Finish() noexcept {
    Completed();
    End();
  }
  void End() noexcept {
    Notify(ExecutionProgressEvent::kEnd);
    observer_ = {};
  }

 private:
  void Notify(ExecutionProgressEvent event) noexcept {
    if (observer_.notify) observer_.notify(observer_.context, kind_, event);
  }
  ExecutionActivityKind kind_;
  ExecutionProgressObserver observer_;
};

}  // namespace q4t::runtime
