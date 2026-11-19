#include "q4t/server/realtime_semantic_turn.h"

namespace q4t::server {
RealtimeSemanticTurn::RealtimeSemanticTurn(uint64_t turn, uint64_t begin_ticks,
                                           std::string_view eagerness)
    : turn_(turn), gate_(eagerness), valid_(gate_.Begin(turn, begin_ticks)) {}

bool RealtimeSemanticTurn::Observe(uint64_t end_ticks, bool speech) {
  const std::lock_guard lock(mutex_);
  if (!valid_ || closed_ || !gate_.Observe(end_ticks, speech)) return false;
  if (speech && control_) control_->Cancel();
  return true;
}
void RealtimeSemanticTurn::Close() {
  const std::lock_guard lock(mutex_);
  if (closed_) return;
  observations_.decision = gate_.Inspect();
  gate_.Finish(turn_);
  closed_ = true;
  if (control_) control_->Cancel();
}
void RealtimeSemanticTurn::CancelScore() {
  const std::lock_guard lock(mutex_);
  if (control_) control_->Cancel();
}
bool RealtimeSemanticTurn::TryScore(uint64_t revision, uint64_t covered_ticks,
                                    std::shared_ptr<RequestControl> control,
                                    RealtimeSemanticTicket* ticket) {
  const std::lock_guard lock(mutex_);
  if (!valid_ || closed_ || observations_.attempts == kRealtimeSemanticSteps ||
      !control || control->Cancelled() ||
      !gate_.RequestScore(revision, covered_ticks, ticket))
    return false;
  ticket_ = *ticket;
  control_ = std::move(control);
  ++observations_.attempts;
  return true;
}
RealtimeSemanticScore RealtimeSemanticTurn::Complete(
    const RealtimeSemanticTicket& ticket, std::optional<double> probability) {
  const std::lock_guard lock(mutex_);
  if (!control_ || ticket != ticket_) return RealtimeSemanticScore::kInvalid;
  // A cancelled inference cannot install a probability, even if it had
  // generated a complete JSON value just before cancellation won.
  if (control_->Cancelled()) probability.reset();
  const auto result = gate_.CompleteScore(ticket, probability);
  control_.reset();
  switch (result) {
    case RealtimeSemanticScore::kAccepted:
      ++observations_.accepted;
      break;
    case RealtimeSemanticScore::kUnavailable:
      ++observations_.unavailable;
      break;
    case RealtimeSemanticScore::kStale:
      ++observations_.stale;
      break;
    case RealtimeSemanticScore::kInvalid:
      ++observations_.invalid;
      break;
  }
  return result;
}
RealtimeSemanticObservations RealtimeSemanticTurn::Inspect() const {
  const std::lock_guard lock(mutex_);
  auto value = observations_;
  if (!closed_) value.decision = gate_.Inspect();
  return value;
}
}  // namespace q4t::server
