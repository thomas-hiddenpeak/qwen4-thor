// A single streaming ASR worker shares this bounded turn with the input owner.
#pragma once

#include <memory>
#include <mutex>

#include "q4t/server/realtime_semantic_vad.h"
#include "q4t/server/request_control.h"

namespace q4t::server {
inline constexpr uint64_t kRealtimeSemanticSteps = 8;
inline constexpr uint64_t kRealtimeSemanticStepTokens = 16;
inline constexpr uint64_t kRealtimeSemanticOutputTokens =
    kRealtimeSemanticSteps * kRealtimeSemanticStepTokens;
struct RealtimeSemanticObservations {
  uint64_t attempts = 0, accepted = 0, unavailable = 0, stale = 0, invalid = 0;
  RealtimeSemanticDecision decision;
};

class RealtimeSemanticTurn {
 public:
  RealtimeSemanticTurn(uint64_t turn, uint64_t begin_ticks,
                       std::string_view eagerness);
  bool Valid() const { return valid_; }
  // Input-owner methods. A resumed voice cancels only the obsolete classifier,
  // not the ongoing transcription. No release before its execution returns.
  bool Observe(uint64_t end_ticks, bool speech);
  void Close();
  void CancelScore();
  // Worker methods, called serially after a real, complete ASR hypothesis.
  bool TryScore(uint64_t revision, uint64_t covered_ticks,
                std::shared_ptr<RequestControl> control,
                RealtimeSemanticTicket* ticket);
  RealtimeSemanticScore Complete(const RealtimeSemanticTicket& ticket,
                                 std::optional<double> probability);
  RealtimeSemanticObservations Inspect() const;

 private:
  const uint64_t turn_;
  RealtimeSemanticVad gate_;
  const bool valid_;
  mutable std::mutex mutex_;
  std::shared_ptr<RequestControl> control_;
  RealtimeSemanticTicket ticket_;
  RealtimeSemanticObservations observations_;
  bool closed_ = false;
};
}  // namespace q4t::server
