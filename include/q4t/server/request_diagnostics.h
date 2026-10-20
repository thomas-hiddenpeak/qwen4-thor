// Handler-thread diagnostics only. No scheduling or CUDA synchronization.
#pragma once

#include <array>
#include <atomic>
#include <chrono>
#include <cstdio>
#include <mutex>
#include <string>

namespace q4t::server {

class RequestDiagnostics {
 public:
  using Clock = std::chrono::steady_clock;
  enum class Wait { kSlot, kModel, kTokenizer, kScheduler, kWrite, kCount };
  class Scope {
   public:
    Scope(RequestDiagnostics& owner, Wait kind)
        : owner_(owner), kind_(kind), start_(Clock::now()) {}
    ~Scope() { owner_.Observe(kind_, Clock::now() - start_); }
    Scope(const Scope&) = delete;
    Scope& operator=(const Scope&) = delete;
   private:
    RequestDiagnostics& owner_;
    Wait kind_;
    Clock::time_point start_;
  };

  explicit RequestDiagnostics(FILE* sink = stderr)
      : instance_(next_.fetch_add(1, std::memory_order_relaxed)), sink_(sink) {
    Event("begin");
  }
  ~RequestDiagnostics() {
    std::fprintf(sink_,
        "[q4t][request] instance=%llu id=%s event=end stage=%s seq=%d "
        "released=%d outcome=%s reason=%s elapsed_ms=%.3f "
        "slot_wait_ms=%.3f model_lock_ms=%.3f tokenizer_lock_ms=%.3f "
        "scheduler_completion_ms=%.3f stream_write_ms=%.3f "
        "scheduler_waits=%llu\n",
        instance_, id_.c_str(), stage_, seq_, released_, outcome_, reason_,
        Milliseconds(Clock::now() - start_), totals_[0], totals_[1],
        totals_[2], totals_[3], totals_[4], counts_[3]);
  }
  void Bind(const std::string& id) { id_ = id; Event("registered"); }
  void Stage(const char* value) { stage_ = value; Event("stage"); }
  void Admitted(int seq) { seq_ = seq; Event("admitted"); }
  void Released() { released_ = true; }
  void Outcome(const char* value, const char* reason) {
    outcome_ = value;
    reason_ = reason;
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
  double Total(Wait kind) const { return totals_[static_cast<size_t>(kind)]; }

 private:
  static double Milliseconds(Clock::duration elapsed) {
    return std::chrono::duration<double, std::milli>(elapsed).count();
  }
  void Event(const char* event) {
    std::fprintf(sink_,
        "[q4t][request] instance=%llu id=%s event=%s stage=%s seq=%d "
        "elapsed_ms=%.3f\n", instance_, id_.c_str(), event, stage_, seq_,
        Milliseconds(Clock::now() - start_));
  }
  static inline std::atomic<unsigned long long> next_{0};
  const unsigned long long instance_;
  FILE* sink_;
  Clock::time_point start_ = Clock::now();
  std::string id_ = "unregistered";
  const char* stage_ = "validation";
  const char* outcome_ = "error";
  const char* reason_ = "handler_error";
  int seq_ = -1;
  bool released_ = false;
  std::array<double, static_cast<size_t>(Wait::kCount)> totals_{};
  std::array<unsigned long long, static_cast<size_t>(Wait::kCount)> counts_{};
};

}  // namespace q4t::server
