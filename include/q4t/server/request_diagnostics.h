// Handler-thread diagnostics only. No scheduling or CUDA synchronization.
#pragma once

#include <array>
#include <algorithm>
#include <atomic>
#include <chrono>
#include <cstdio>
#include <mutex>
#include <string>
#include <string_view>

#include "q4t/server/request_control.h"
#include "q4t/server/progress_context.h"
#include "q4t/server/request_progress.h"

namespace q4t::server {

class RequestDiagnostics {
 public:
  using Clock = std::chrono::steady_clock;
  enum class Wait {
    kSlot,
    kModel,
    kTokenizer,
    kScheduler,
    kWrite,
    kGrammar,
    kMedia,
    kStorage,
    kCache,
    kInput,
    kCount
  };
  class Scope {
   public:
    Scope(RequestDiagnostics& owner, Wait kind)
        : owner_(owner),
          kind_(kind),
          start_(Clock::now()),
          previous_(owner.wait_) {
      owner_.wait_ = kind;
      owner_.progress_.Wait(ToProgressWait(kind));
    }
    ~Scope() {
      owner_.progress_.Wait(ToProgressWait(previous_));
      owner_.wait_ = previous_;
      owner_.Observe(kind_, Clock::now() - start_);
    }
    Scope(const Scope&) = delete;
    Scope& operator=(const Scope&) = delete;
   private:
    RequestDiagnostics& owner_;
    Wait kind_;
    Clock::time_point start_;
    Wait previous_;
  };

  // A real device operation inside an auxiliary wait is executing work. Keep
  // the outer operation identity, but expose its actual wait/stage until the
  // call returns. Scope exit (including failure) never claims completion.
  class ExecutionScope {
   public:
    ExecutionScope(RequestDiagnostics& owner, ProgressStage stage)
        : owner_(owner),
          previous_wait_(owner.wait_),
          previous_stage_(owner.progress_.StageValue()),
          execution_(ProgressWork::kCache) {
      owner_.wait_ = Wait::kCount;
      owner_.progress_.Wait(ProgressWait::kNone);
      owner_.progress_.Stage(stage);
    }
    ~ExecutionScope() {
      owner_.progress_.Stage(previous_stage_);
      owner_.wait_ = previous_wait_;
      owner_.progress_.Wait(ToProgressWait(previous_wait_));
    }
    ExecutionScope(const ExecutionScope&) = delete;
    ExecutionScope& operator=(const ExecutionScope&) = delete;
    void Completed() { ProgressExecution::Completed(); }

   private:
    RequestDiagnostics& owner_;
    Wait previous_wait_;
    ProgressStage previous_stage_;
    ProgressExecution execution_;
  };

  explicit RequestDiagnostics(FILE* sink = stderr, std::string trace_id = "-")
      : instance_(next_.fetch_add(1, std::memory_order_relaxed)),
        sink_(sink),
        trace_id_(std::move(trace_id)) {
    Event("begin");
  }
  ~RequestDiagnostics() {
    std::fprintf(
        sink_,
        "[q4t][request] instance=%llu id=%s event=end stage=%s seq=%d "
        "released=%d outcome=%s reason=%s elapsed_ms=%.3f "
        "slot_wait_ms=%.3f model_lock_ms=%.3f tokenizer_lock_ms=%.3f "
        "scheduler_completion_ms=%.3f stream_write_ms=%.3f input_wait_ms=%.3f "
        "scheduler_waits=%llu trace_id=%s safety_identifier_sha256=%s\n",
        instance_, id_.c_str(), stage_, seq_, released_, outcome_, reason_,
        Milliseconds(Clock::now() - start_), totals_[0], totals_[1], totals_[2],
        totals_[3], totals_[4], totals_[9], counts_[3], trace_id_.c_str(),
        attribution_.c_str());
  }
  void Bind(const std::string& id) { id_ = id; Event("registered"); }
  // Only a fixed digest may enter diagnostics, never caller-controlled text.
  bool BindAttribution(std::string digest) {
    if (!digest.empty() &&
        (digest.size() != 64 ||
         !std::all_of(digest.begin(), digest.end(), [](unsigned char c) {
           return (c >= '0' && c <= '9') || (c >= 'a' && c <= 'f');
         })))
      return false;
    attribution_ = digest.empty() ? "-" : std::move(digest);
    return true;
  }
  void Stage(std::string_view value) {
    CopyLabel(stage_, value);
    const auto name = value;
    progress_stage_ =
        name == "prefill"           ? ProgressStage::kPrefill
        : name == "decode"          ? ProgressStage::kDecode
        : name == "response"        ? ProgressStage::kOutput
        : name == "media"           ? ProgressStage::kMedia
        : name == "grammar_compile" ? ProgressStage::kGrammar
        : name == "storage"         ? ProgressStage::kStorage
        : name == "cleanup"         ? ProgressStage::kCleanup
        : name == "slot_admission" || name == "structured_admission"
            ? ProgressStage::kAdmission
            : ProgressStage::kInput;
    progress_.Stage(progress_stage_);
    Event("stage");
  }
  void BindProgress(ProgressHandle progress) { progress_ = progress; }
  ProgressWork Work() const {
    if (progress_stage_ == ProgressStage::kPrefill)
      return ProgressWork::kPrefill;
    if (progress_stage_ == ProgressStage::kDecode) return ProgressWork::kDecode;
    if (progress_stage_ == ProgressStage::kMedia) return ProgressWork::kMedia;
    return ProgressWork::kCache;
  }
  void Admitted(int seq) { seq_ = seq; Event("admitted"); }
  void Released() { released_ = true; }
  void Outcome(std::string_view value, std::string_view reason) noexcept {
    CopyLabel(outcome_, value);
    CopyLabel(reason_, reason);
  }
  void Observe(Wait kind, Clock::duration elapsed) {
    const auto i = static_cast<size_t>(kind);
    totals_[i] += Milliseconds(elapsed);
    ++counts_[i];
  }
  std::unique_lock<std::mutex> Lock(std::mutex& mutex, Wait kind) {
    Scope wait(*this, kind);
    return std::unique_lock<std::mutex>(mutex);
  }
  template <typename Cancelled>
  std::unique_lock<std::mutex> Lock(std::mutex& mutex, Wait kind,
                                  const Cancelled& cancelled) {
    Scope wait(*this, kind);
    return LockUnlessCancelled(mutex, cancelled);
  }
  double Total(Wait kind) const { return totals_[static_cast<size_t>(kind)]; }

 private:
  // Request scopes often outlive the temporary error that supplied a label.
  // Own bounded copies without allocating on a business failure path. Never
  // turn an oversized label into a truncated but apparently valid error code.
  template <size_t N>
  static void CopyLabel(char (&out)[N], std::string_view value) noexcept {
    constexpr std::string_view overflow = "diagnostic_label_too_long";
    static_assert(N > overflow.size());
    if (value.size() >= N) value = overflow;
    std::copy(value.begin(), value.end(), out);
    out[value.size()] = '\0';
  }
  static ProgressWait ToProgressWait(Wait wait) {
    switch (wait) {
      case Wait::kSlot:
        return ProgressWait::kSlot;
      case Wait::kModel:
        return ProgressWait::kModel;
      case Wait::kTokenizer:
        return ProgressWait::kTokenizer;
      case Wait::kScheduler:
        return ProgressWait::kScheduler;
      case Wait::kWrite:
        return ProgressWait::kWrite;
      case Wait::kGrammar:
        return ProgressWait::kGrammar;
      case Wait::kMedia:
        return ProgressWait::kMedia;
      case Wait::kStorage:
        return ProgressWait::kStorage;
      case Wait::kCache:
        return ProgressWait::kCache;
      case Wait::kInput:
        return ProgressWait::kInput;
      case Wait::kCount:
        return ProgressWait::kNone;
    }
    return ProgressWait::kNone;
  }
  ProgressHandle progress_;
  ProgressStage progress_stage_ = ProgressStage::kInput;
  Wait wait_ = Wait::kCount;
  static double Milliseconds(Clock::duration elapsed) {
    return std::chrono::duration<double, std::milli>(elapsed).count();
  }
  void Event(const char* event) {
    std::fprintf(sink_,
                 "[q4t][request] instance=%llu id=%s event=%s stage=%s seq=%d "
                 "elapsed_ms=%.3f trace_id=%s\n",
                 instance_, id_.c_str(), event, stage_, seq_,
                 Milliseconds(Clock::now() - start_), trace_id_.c_str());
  }
  static inline std::atomic<unsigned long long> next_{0};
  const unsigned long long instance_;
  FILE* sink_;
  const std::string trace_id_;
  Clock::time_point start_ = Clock::now();
  std::string id_ = "unregistered";
  std::string attribution_ = "-";
  char stage_[128] = "validation";
  char outcome_[128] = "error";
  char reason_[128] = "handler_error";
  int seq_ = -1;
  bool released_ = false;
  std::array<double, static_cast<size_t>(Wait::kCount)> totals_{};
  std::array<unsigned long long, static_cast<size_t>(Wait::kCount)> counts_{};
};

}  // namespace q4t::server
