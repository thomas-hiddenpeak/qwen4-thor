#include "q4t/server/realtime_semantic_vad.h"

#include <cmath>
#include <limits>

#include "q4t/server/realtime_audio_queue.h"

namespace q4t::server {
namespace {
constexpr uint64_t kTicksPerMs = kRealtimeTicksPerSecond / 1000;
}  // namespace

uint32_t RealtimeSemanticMaximumWaitMs(std::string_view eagerness) {
  if (eagerness == "low") return 8000;
  if (eagerness == "medium" || eagerness == "auto") return 4000;
  if (eagerness == "high") return 2000;
  return 0;
}
RealtimeSemanticVad::RealtimeSemanticVad(std::string_view eagerness,
                                         uint32_t minimum_silence_ms)
    : maximum_ms_(RealtimeSemanticMaximumWaitMs(eagerness)),
      minimum_ms_(minimum_silence_ms),
      valid_(maximum_ms_ != 0 && minimum_ms_ != 0 &&
             minimum_ms_ <= maximum_ms_) {}

bool RealtimeSemanticVad::Begin(uint64_t turn, uint64_t begin_ticks) {
  if (!valid_ || active_ || turn == 0 || turn <= turn_ ||
      begin_ticks < cursor_ || begin_ticks > kRealtimeSessionTicks)
    return false;
  turn_ = turn;
  cursor_ = speech_end_ = begin_ticks;
  active_ = true;
  heard_speech_ = quiet_ = attempted_ = false;
  probability_.reset();
  return true;
}
bool RealtimeSemanticVad::Observe(uint64_t end_ticks, bool speech) {
  if (!valid_ || !active_ || end_ticks <= cursor_ ||
      end_ticks > kRealtimeSessionTicks ||
      (!speech && heard_speech_ && !quiet_ &&
       quiet_epoch_ == std::numeric_limits<uint64_t>::max()))
    return false;
  if (speech) {
    heard_speech_ = true;
    speech_end_ = end_ticks;
    quiet_ = attempted_ = false;
    probability_.reset();
  } else if (heard_speech_ && !quiet_) {
    quiet_ = true;
    quiet_begin_ = cursor_;
    ++quiet_epoch_;
  }
  cursor_ = end_ticks;
  return true;
}
bool RealtimeSemanticVad::RequestScore(uint64_t transcript_revision,
                                       uint64_t covered_ticks,
                                       RealtimeSemanticTicket* ticket) {
  if (!valid_ || !active_ || !quiet_ || attempted_ || in_flight_ || !ticket ||
      transcript_revision == 0 || covered_ticks < speech_end_ ||
      covered_ticks > cursor_ ||
      cursor_ - quiet_begin_ < minimum_ms_ * kTicksPerMs ||
      cursor_ - quiet_begin_ >= maximum_ms_ * kTicksPerMs ||
      sequence_ == std::numeric_limits<uint64_t>::max())
    return false;
  *ticket = {++sequence_, turn_, quiet_epoch_, speech_end_,
             transcript_revision};
  in_flight_ = *ticket;
  attempted_ = true;
  return true;
}
RealtimeSemanticScore RealtimeSemanticVad::CompleteScore(
    const RealtimeSemanticTicket& ticket, std::optional<double> probability) {
  if (!in_flight_ || *in_flight_ != ticket)
    return RealtimeSemanticScore::kInvalid;
  in_flight_.reset();
  if (!active_ || !quiet_ || ticket.turn != turn_ ||
      ticket.quiet != quiet_epoch_ || ticket.speech_end_ticks != speech_end_)
    return RealtimeSemanticScore::kStale;
  if (!probability) return RealtimeSemanticScore::kUnavailable;
  if (!std::isfinite(*probability) || *probability < 0 || *probability > 1)
    return RealtimeSemanticScore::kInvalid;
  probability_ = *probability;
  return RealtimeSemanticScore::kAccepted;
}
bool RealtimeSemanticVad::Finish(uint64_t turn) {
  if (!valid_ || !active_ || turn != turn_) return false;
  active_ = heard_speech_ = quiet_ = attempted_ = false;
  probability_.reset();
  return true;
}
RealtimeSemanticDecision RealtimeSemanticVad::Inspect() const {
  using K = RealtimeSemanticDecision::Kind;
  if (!valid_ || !active_) return {};
  if (!quiet_) return {K::kAwaitingSilence};
  // Project policy: interpolate a bounded delay from the actual classifier
  // probability. Round up to milliseconds, preserving all supported sample
  // grids. This is not a claim to reproduce OpenAI's proprietary classifier.
  const uint64_t delay_ms =
      probability_
          ? uint64_t(std::ceil(minimum_ms_ + (maximum_ms_ - minimum_ms_) *
                                                 (1 - *probability_)))
          : maximum_ms_;
  const auto deadline = quiet_begin_ + delay_ms * kTicksPerMs;
  const bool maximum = cursor_ >= quiet_begin_ + maximum_ms_ * kTicksPerMs;
  return {cursor_ >= deadline ? K::kEnd : K::kWaiting, deadline,
          probability_.has_value(), maximum};
}
}  // namespace q4t::server
