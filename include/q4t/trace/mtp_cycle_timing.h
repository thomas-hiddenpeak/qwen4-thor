#pragma once

#include <array>
#include <chrono>
#include <cstddef>
#include <cstdint>
#include <string>
#include <string_view>
#include <vector>

namespace q4t::trace {

// Pure host, request-owned diagnostics. No CUDA APIs or changes to completion.
// Default-null TLS performs no clock reads. Spans include existing submission
// and waiting time; nested detail intervals must never be added to parents.
enum class CyclePhase { kDraft, kVerify, kAccept, kExtend, kCount };
enum class CycleDetail {
  kPositionsReadback,
  kDraftMoeCounts,
  kVerifyMoeCounts,
  kLinearAlloc,
  kLinearFree,
  kGatherAlloc,
  kGatherFree,
  kMtpForward,
  kMtpAttention,
  kMtpMoe,
  kMtpHead,
  kCount,
};

class MtpCycleTiming;
class MtpCycleScope;
class MtpCycleSpan;

class MtpCycleStep {
 public:
  using Clock = std::chrono::steady_clock;
  void Mark(const char* name) noexcept;
  void Invalidate(const char* reason) noexcept;
  void SetEngineResult(int accepted_drafts, int returned, int verify_rows,
                       int extend_rows) noexcept;
  // Counts for this step, after EOS/output-budget clipping. Writes count only
  // successful SSE content-chunk writes; nonempty_writes is a subset of them.
  void SetDelivery(int generated, int token_piece_writes,
                   int nonempty_writes) noexcept;

 private:
  friend class MtpCycleTiming;
  friend class MtpCycleScope;
  friend class MtpCycleSpan;
  static constexpr size_t kMaxMarks = 64;
  static constexpr size_t kPhases = static_cast<size_t>(CyclePhase::kCount);
  static constexpr size_t kDetails = static_cast<size_t>(CycleDetail::kCount);
  struct MarkValue {
    const char* name = nullptr;
    double at_ms = 0;
  };
  struct DetailValue {
    uint64_t calls = 0;
    double host_ms = 0;
  };
  MtpCycleTiming* owner_ = nullptr;
  int position_ = 0;
  int k_ = 0;
  CyclePhase phase_ = CyclePhase::kDraft;
  std::array<MarkValue, kMaxMarks> marks_{};
  size_t mark_count_ = 0;
  std::array<std::array<DetailValue, kDetails>, kPhases> details_{};
  int accepted_drafts_ = -1;
  int returned_ = -1;
  int verify_rows_ = -1;
  int extend_rows_ = -1;
  int generated_ = -1;
  int token_piece_writes_ = -1;
  int nonempty_writes_ = -1;
};

class MtpCycleScope {
 public:
  explicit MtpCycleScope(MtpCycleStep* step) noexcept;
  ~MtpCycleScope() noexcept;
  MtpCycleScope(const MtpCycleScope&) = delete;
  MtpCycleScope& operator=(const MtpCycleScope&) = delete;
  void SetPhase(CyclePhase phase) noexcept;

 private:
  MtpCycleStep* step_;
  MtpCycleStep* previous_;
  CyclePhase previous_phase_ = CyclePhase::kDraft;
};

class MtpCycleSpan {
 public:
  explicit MtpCycleSpan(CycleDetail detail) noexcept;
  ~MtpCycleSpan() noexcept;
  MtpCycleSpan(const MtpCycleSpan&) = delete;
  MtpCycleSpan& operator=(const MtpCycleSpan&) = delete;

 private:
  MtpCycleStep* step_;
  CyclePhase phase_ = CyclePhase::kDraft;
  CycleDetail detail_;
  MtpCycleStep::Clock::time_point begin_{};
};

class MtpCycleTiming {
 public:
  using Clock = std::chrono::steady_clock;
  MtpCycleTiming(std::string response_id, int prompt_tokens, int max_tokens,
                 int k, Clock::time_point origin);
  ~MtpCycleTiming() noexcept;
  MtpCycleTiming(const MtpCycleTiming&) = delete;
  MtpCycleTiming& operator=(const MtpCycleTiming&) = delete;
  MtpCycleStep* BeginStep(int position, int k) noexcept;
  void MarkRequest(const char* name) noexcept;
  void Finish(int generated, int mtp_steps, bool plain_tail,
              std::string_view finish_reason, std::string_view fallback,
              bool success) noexcept;
  bool FirstContentWritten() const noexcept { return first_content_written_; }
  void FinishFirstContent() noexcept;

 private:
  friend class MtpCycleStep;
  static constexpr size_t kMaxSteps = 1024;
  static constexpr size_t kMaxRequestMarks = 64;
  void Fail(const char* reason) noexcept;
  double NowMs() const noexcept;
  void Report();
  std::string response_id_;
  int prompt_tokens_;
  int max_tokens_;
  int k_;
  Clock::time_point origin_;
  std::vector<MtpCycleStep> steps_;
  size_t step_count_ = 0;
  std::array<MtpCycleStep::MarkValue, kMaxRequestMarks> marks_{};
  size_t mark_count_ = 0;
  const char* error_ = nullptr;
  bool first_content_written_ = false;
  bool finished_ = false;
  bool success_ = false;
  int generated_ = -1;
  int mtp_steps_ = -1;
  bool plain_tail_ = false;
  std::string finish_reason_;
  std::string fallback_;
  double setup_host_ms_ = 0;
};

}  // namespace q4t::trace
