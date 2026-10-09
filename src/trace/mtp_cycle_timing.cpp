#include "q4t/trace/mtp_cycle_timing.h"

#include <algorithm>
#include <cmath>
#include <cstdio>
#include <cstring>
#include <iomanip>
#include <sstream>
#include <utility>

namespace q4t::trace {
namespace {
thread_local MtpCycleStep* active_step = nullptr;

constexpr const char* kPhaseNames[] = {"draft", "verify", "accept", "extend"};
constexpr const char* kDetailNames[] = {"positions_readback",
                                        "draft_moe_counts",
                                        "verify_moe_counts",
                                        "linear_alloc",
                                        "linear_free",
                                        "gather_alloc",
                                        "gather_free",
                                        "mtp_forward",
                                        "mtp_attention",
                                        "mtp_moe",
                                        "mtp_head"};
constexpr const char* kRequestMarks[] = {"timing_setup_begin",
                                         "timing_setup_end",
                                         "main_prefill_finished",
                                         "init_finished",
                                         "first_content_write_begin",
                                         "first_content_write_end",
                                         "request_end"};
constexpr const char* kStepMarks[] = {"submit",
                                      "scheduler_pick",
                                      "model_lock_acquired",
                                      "engine_begin",
                                      "draft_end",
                                      "verify_pack_end",
                                      "verify_model_end",
                                      "verify_readback_end",
                                      "accept_end",
                                      "extend_pack_end",
                                      "extend_gather_end",
                                      "extend_model_end",
                                      "extend_readback_end",
                                      "engine_end",
                                      "scheduler_finish",
                                      "request_wake",
                                      "emit_begin",
                                      "emit_end",
                                      "advance_end"};

void Quote(std::ostream& out, const char* value) {
  out << '"';
  for (const unsigned char* p = reinterpret_cast<const unsigned char*>(value);
       *p; ++p) {
    if (*p == '"' || *p == '\\')
      out << '\\' << static_cast<char>(*p);
    else if (*p < 32) {
      constexpr char kHex[] = "0123456789abcdef";
      out << "\\u00" << kHex[*p >> 4] << kHex[*p & 15];
    } else
      out << static_cast<char>(*p);
  }
  out << '"';
}
}  // namespace

void MtpCycleStep::Mark(const char* name) noexcept {
  if (!owner_) return;
  if (!name || mark_count_ == marks_.size()) {
    Invalidate(!name ? "null_step_mark" : "step_mark_capacity");
    return;
  }
  marks_[mark_count_++] = {name, owner_->NowMs()};
}

void MtpCycleStep::Invalidate(const char* reason) noexcept {
  if (owner_) owner_->Fail(reason);
}

void MtpCycleStep::SetEngineResult(int accepted_drafts, int returned,
                                   int verify_rows, int extend_rows) noexcept {
  if (accepted_drafts_ >= 0) Invalidate("duplicate_engine_result");
  accepted_drafts_ = accepted_drafts;
  returned_ = returned;
  verify_rows_ = verify_rows;
  extend_rows_ = extend_rows;
  if (accepted_drafts < 0 || accepted_drafts > k_ ||
      returned != accepted_drafts + 1 || verify_rows != k_ + 1 ||
      extend_rows != returned)
    Invalidate("engine_result_contract");
}

void MtpCycleStep::SetDelivery(int generated, int token_piece_writes,
                               int nonempty_writes) noexcept {
  if (generated_ >= 0) Invalidate("duplicate_delivery");
  generated_ = generated;
  token_piece_writes_ = token_piece_writes;
  nonempty_writes_ = nonempty_writes;
  if (generated < 0 || generated > returned_ || token_piece_writes < 0 ||
      token_piece_writes > generated || nonempty_writes < 0 ||
      nonempty_writes > token_piece_writes)
    Invalidate("delivery_contract");
}

MtpCycleScope::MtpCycleScope(MtpCycleStep* step) noexcept
    : step_(step), previous_(active_step) {
  if (previous_) previous_phase_ = previous_->phase_;
  active_step = step;
}

MtpCycleScope::~MtpCycleScope() noexcept {
  if (previous_) previous_->phase_ = previous_phase_;
  active_step = previous_;
}

void MtpCycleScope::SetPhase(CyclePhase phase) noexcept {
  if (!step_) return;
  if (static_cast<size_t>(phase) >= MtpCycleStep::kPhases) {
    step_->Invalidate("invalid_phase");
    return;
  }
  step_->phase_ = phase;
}

MtpCycleSpan::MtpCycleSpan(CycleDetail detail) noexcept
    : step_(active_step), detail_(detail) {
  if (!step_) return;
  if (static_cast<size_t>(detail) >= MtpCycleStep::kDetails) {
    step_->Invalidate("invalid_detail");
    step_ = nullptr;
    return;
  }
  phase_ = step_->phase_;
  begin_ = MtpCycleStep::Clock::now();
}

MtpCycleSpan::~MtpCycleSpan() noexcept {
  if (!step_) return;
  const double elapsed = std::chrono::duration<double, std::milli>(
                             MtpCycleStep::Clock::now() - begin_)
                             .count();
  if (!std::isfinite(elapsed) || elapsed < 0) {
    step_->Invalidate("detail_nonfinite");
    return;
  }
  auto& value =
      step_
          ->details_[static_cast<size_t>(phase_)][static_cast<size_t>(detail_)];
  ++value.calls;
  value.host_ms += elapsed;
  if (!std::isfinite(value.host_ms)) step_->Invalidate("detail_nonfinite");
}

MtpCycleTiming::MtpCycleTiming(std::string response_id, int prompt_tokens,
                               int max_tokens, int k, Clock::time_point origin)
    : response_id_(std::move(response_id)),
      prompt_tokens_(prompt_tokens),
      max_tokens_(max_tokens),
      k_(k),
      origin_(origin) {
  const auto begin = Clock::now();
  MarkRequest("timing_setup_begin");
  if (prompt_tokens <= 0 || max_tokens <= 0 || k <= 0) {
    Fail("invalid_shape");
  } else {
    steps_.resize(std::min(static_cast<size_t>(max_tokens), kMaxSteps));
  }
  MarkRequest("timing_setup_end");
  setup_host_ms_ =
      std::chrono::duration<double, std::milli>(Clock::now() - begin).count();
}

MtpCycleTiming::~MtpCycleTiming() noexcept {
  bool has_request_end = false;
  for (size_t i = 0; i < mark_count_; ++i)
    if (std::strcmp(marks_[i].name, "request_end") == 0) has_request_end = true;
  if (!has_request_end) MarkRequest("request_end");
  try {
    Report();
  } catch (...) {
    // response_id is restricted to alnum/-/_ by the HTTP request contract.
    std::fprintf(stderr,
                 "[q4t][mtp_cycle_timing] {\"schema_version\":1,"
                 "\"response_id\":\"%s\",\"complete\":false,\"valid\":false,"
                 "\"error\":\"report_exception\"}\n",
                 response_id_.c_str());
  }
}

void MtpCycleTiming::Fail(const char* reason) noexcept {
  if (!error_) error_ = reason;
}

double MtpCycleTiming::NowMs() const noexcept {
  return std::chrono::duration<double, std::milli>(Clock::now() - origin_)
      .count();
}

MtpCycleStep* MtpCycleTiming::BeginStep(int position, int k) noexcept {
  if (step_count_ == steps_.size()) {
    Fail("step_capacity");
    return nullptr;
  }
  auto& step = steps_[step_count_++];
  step.owner_ = this;
  step.position_ = position;
  step.k_ = k;
  if (position < prompt_tokens_ || k != k_) Fail("step_shape");
  return &step;
}

void MtpCycleTiming::MarkRequest(const char* name) noexcept {
  if (!name || mark_count_ == marks_.size()) {
    Fail(!name ? "null_request_mark" : "request_mark_capacity");
    return;
  }
  marks_[mark_count_++] = {name, NowMs()};
}

void MtpCycleTiming::Finish(int generated, int mtp_steps, bool plain_tail,
                            std::string_view finish_reason,
                            std::string_view fallback, bool success) noexcept {
  if (finished_) Fail("duplicate_finish");
  finished_ = true;
  generated_ = generated;
  mtp_steps_ = mtp_steps;
  plain_tail_ = plain_tail;
  success_ = success;
  try {
    finish_reason_.assign(finish_reason);
    fallback_.assign(fallback);
  } catch (...) {
    Fail("finish_exception");
  }
}

void MtpCycleTiming::FinishFirstContent() noexcept {
  if (!first_content_written_) {
    first_content_written_ = true;
    MarkRequest("first_content_write_end");
  }
}

void MtpCycleTiming::Report() {
  const auto report_begin = Clock::now();
  std::ostringstream body;
  body << std::setprecision(17) << std::boolalpha;
  auto marks = [&](const auto& values, size_t count) {
    body << '[';
    double previous = -1;
    for (size_t i = 0; i < count; ++i) {
      if (i) body << ',';
      if (!std::isfinite(values[i].at_ms) || values[i].at_ms < 0 ||
          values[i].at_ms < previous)
        Fail("mark_order_or_nonfinite");
      previous = values[i].at_ms;
      body << "{\"name\":";
      Quote(body, values[i].name);
      body << ",\"at_ms\":";
      if (std::isfinite(values[i].at_ms))
        body << values[i].at_ms;
      else
        body << "null";
      body << '}';
    }
    body << ']';
  };
  body << ",\"request_marks\":";
  marks(marks_, mark_count_);
  auto names_match = [](const auto& values, size_t count,
                        const auto& expected) {
    if (count != sizeof(expected) / sizeof(expected[0])) return false;
    for (size_t i = 0; i < count; ++i)
      if (std::strcmp(values[i].name, expected[i]) != 0) return false;
    return true;
  };
  const bool request_complete = names_match(marks_, mark_count_, kRequestMarks);
  if (!request_complete) Fail("request_marks_incomplete");
  body << ",\"steps\":[";
  bool steps_complete = true;
  int delivered_total = 0;
  for (size_t i = 0; i < step_count_; ++i) {
    const auto& step = steps_[i];
    if (i) body << ',';
    if (!names_match(step.marks_, step.mark_count_, kStepMarks) ||
        step.accepted_drafts_ < 0 || step.generated_ < 0)
      steps_complete = false;
    if (step.generated_ >= 0) delivered_total += step.generated_;
    body << "{\"index\":" << i << ",\"position\":" << step.position_
         << ",\"k\":" << step.k_
         << ",\"accepted_drafts\":" << step.accepted_drafts_
         << ",\"returned\":" << step.returned_
         << ",\"verify_rows\":" << step.verify_rows_
         << ",\"extend_rows\":" << step.extend_rows_
         << ",\"generated\":" << step.generated_
         << ",\"token_piece_writes\":" << step.token_piece_writes_
         << ",\"nonempty_writes\":" << step.nonempty_writes_ << ",\"marks\":";
    marks(step.marks_, step.mark_count_);
    body << ",\"details\":[";
    bool comma = false;
    for (size_t p = 0; p < MtpCycleStep::kPhases; ++p) {
      for (size_t d = 0; d < MtpCycleStep::kDetails; ++d) {
        const auto& value = step.details_[p][d];
        if (comma) body << ',';
        comma = true;
        body << "{\"phase\":";
        Quote(body, kPhaseNames[p]);
        body << ",\"name\":";
        Quote(body, kDetailNames[d]);
        body << ",\"calls\":" << value.calls << ",\"host_ms\":";
        if (std::isfinite(value.host_ms))
          body << value.host_ms;
        else
          body << "null";
        body << '}';
      }
    }
    body << "]}";
  }
  body << ']';
  if (finished_ &&
      (generated_ < 0 || generated_ > max_tokens_ || mtp_steps_ < 0 ||
       static_cast<size_t>(mtp_steps_) != step_count_ ||
       delivered_total > generated_))
    Fail("request_result_contract");
  // EOS can be the pending bonus token and bypass a new engine step; plain
  // tails also add tokens outside these steps. Exact totals are checked by the
  // offline analyzer with finish_reason/plain_tail and response usage.
  const bool complete = finished_ && success_ && request_complete &&
                        steps_complete && step_count_ > 0 && mtp_steps_ >= 0 &&
                        static_cast<size_t>(mtp_steps_) == step_count_;
  if (!complete) Fail("incomplete");
  std::ostringstream output;
  output << std::setprecision(17) << std::boolalpha
         << "{\"schema_version\":1,\"response_id\":";
  Quote(output, response_id_.c_str());
  output << ",\"prompt_tokens\":" << prompt_tokens_
         << ",\"max_tokens\":" << max_tokens_ << ",\"k\":" << k_
         << ",\"max_seq\":1,\"text_only\":true,\"complete\":" << complete
         << ",\"valid\":" << !error_ << ",\"error\":";
  Quote(output, error_ ? error_ : "");
  output << ",\"success\":" << success_ << ",\"plain_tail\":" << plain_tail_
         << ",\"finish_reason\":";
  Quote(output, finish_reason_.c_str());
  output << ",\"fallback\":";
  Quote(output, fallback_.c_str());
  output << ",\"generated\":" << generated_ << ",\"mtp_steps\":" << mtp_steps_
         << ",\"step_capacity\":" << steps_.size()
         << ",\"diagnostic_only\":true,\"host_origin\":\"handle_chat_entry\""
         << ",\"unit\":\"ms\",\"clock\":\"steady_clock\""
         << ",\"contains_cuda_events\":false,\"host_details_additive\":false"
         << ",\"setup_host_ms\":" << setup_host_ms_ << body.str();
  const double report_host_ms =
      std::chrono::duration<double, std::milli>(Clock::now() - report_begin)
          .count();
  // Excludes this final field serialization, final string copy and fprintf.
  output << ",\"report_host_ms\":" << report_host_ms << '}';
  std::fprintf(stderr, "[q4t][mtp_cycle_timing] %s\n", output.str().c_str());
}

}  // namespace q4t::trace
