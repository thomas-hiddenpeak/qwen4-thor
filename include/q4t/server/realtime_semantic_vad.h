// Bounded semantic turn decisions. The owner supplies actual audio positions
// and model probabilities; this class does not infer silence from idle time.
#pragma once

#include <cstdint>
#include <optional>
#include <string_view>

namespace q4t::server {
uint32_t RealtimeSemanticMaximumWaitMs(std::string_view eagerness);

struct RealtimeSemanticTicket {
  uint64_t sequence = 0, turn = 0, quiet = 0;
  uint64_t speech_end_ticks = 0, transcript_revision = 0;
  bool operator==(const RealtimeSemanticTicket&) const = default;
};
enum class RealtimeSemanticScore { kAccepted, kUnavailable, kStale, kInvalid };
struct RealtimeSemanticDecision {
  enum class Kind { kIdle, kAwaitingSilence, kWaiting, kEnd };
  Kind kind = Kind::kIdle;
  uint64_t end_ticks = 0;
  bool probability_available = false, maximum_wait = false;
};

// Single owner, constant space, no thread or model execution. A score ticket
// stays occupied across speech/clear/turn changes until its exact worker has
// returned. The caller must join that worker before CompleteScore; a stale
// completion cannot free a newer ticket or end a resumed utterance.
class RealtimeSemanticVad {
 public:
  explicit RealtimeSemanticVad(std::string_view eagerness = "auto",
                               uint32_t minimum_silence_ms = 200);
  bool Valid() const { return valid_; }
  bool Begin(uint64_t turn, uint64_t begin_ticks);
  // Each observation covers real, contiguous analyzed audio since the prior
  // cursor. No data must not advance end_ticks, even when the socket is idle.
  bool Observe(uint64_t end_ticks, bool speech);
  // The snapshot must contain the full current ASR hypothesis through the
  // last voiced frame, not merely an earlier stable emitted prefix. Only one
  // model attempt per silence window; failure waits for the absolute bound.
  bool RequestScore(uint64_t transcript_revision, uint64_t covered_ticks,
                    RealtimeSemanticTicket* ticket);
  RealtimeSemanticScore CompleteScore(const RealtimeSemanticTicket& ticket,
                                      std::optional<double> probability);
  bool Finish(uint64_t turn);
  bool ScoreInFlight() const { return in_flight_.has_value(); }
  RealtimeSemanticDecision Inspect() const;

 private:
  const uint32_t maximum_ms_, minimum_ms_;
  const bool valid_;
  uint64_t turn_ = 0, cursor_ = 0, speech_end_ = 0, quiet_begin_ = 0;
  uint64_t quiet_epoch_ = 0, sequence_ = 0;
  bool active_ = false, heard_speech_ = false, quiet_ = false;
  bool attempted_ = false;
  std::optional<double> probability_;
  std::optional<RealtimeSemanticTicket> in_flight_;
};
}  // namespace q4t::server
