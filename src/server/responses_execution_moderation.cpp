#include "q4t/server/responses_execution.h"

#include <algorithm>

#include "object_protocol.h"

namespace q4t::server {
namespace {
using object_detail::Error;
}  // namespace

Status ResponsesExecution::SetModerationResults(const io::Json& results,
                                                const ModerationUsage& usage) {
  auto status = output_.Moderation(results, usage);
  // Preserve safe classification metadata when withholding or discarding
  // generated content. No raw generated draft is copied into this snapshot.
  if (status && moderation_safe_)
    status = moderation_safe_->Moderation(results, usage);
  return status;
}

ObjectReply ResponsesExecution::HoldUnreviewedOutput() {
  const auto* items = output_.Snapshot().GetArray("output");
  if (!begun_ || plan_.finished || commit_unknown_ || moderation_safe_ ||
      !items || !items->array.empty() ||
      output_.Snapshot().GetString("status") != "in_progress")
    return Error(409, "moderation_hold_invalid",
                 "Output hold must start before any generated content");
  moderation_safe_ = output_;
  moderation_safe_events_ = events_.array.size();
  moderation_safe_bytes_ = event_bytes_;
  moderation_wire_.clear();
  return {};
}

ObjectReply ResponsesExecution::PublishReviewedOutput(
    const io::Json& moderation, std::string* wire) {
  wire->clear();
  if (!begun_ || plan_.finished || commit_unknown_ || !moderation_safe_)
    return Error(409, "moderation_hold_invalid",
                 "No releasable private output");
  const auto decision = DecideModeration(ModerationMode::kBlock, moderation);
  if (decision != ModerationDecision::kAllow)
    return Error(decision == ModerationDecision::kBlocked ? 403 : 503,
                 "moderation_not_approved",
                 "Output review did not approve release");
  // Keep the safe snapshot alive through any exception or uncertain durable
  // receipt. No exception renderer may leak the still-private event prefix.
  auto reply = BindMutationResult(
      state_.Checkpoint(output_.Snapshot(), events_, access_, &plan_));
  if (!reply.ok()) return reply;
  checkpoint_time_ = std::chrono::steady_clock::now();
  checkpoint_bytes_ = event_bytes_;
  moderation_safe_.reset();
  *wire = std::move(moderation_wire_);
  moderation_wire_.clear();
  return {};
}

void ResponsesExecution::DiscardUnreviewedOutput() noexcept {
  if (!moderation_safe_) return;
  output_ = std::move(*moderation_safe_);
  moderation_safe_.reset();
  events_.array.resize(std::min(moderation_safe_events_, events_.array.size()));
  event_bytes_ = moderation_safe_bytes_;
  moderation_wire_.clear();
  failure_prefix_.clear();
}
}  // namespace q4t::server
