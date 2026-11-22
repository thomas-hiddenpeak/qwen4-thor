#include "q4t/server/live_responses_validation.h"

#include "object_protocol.h"
#include "q4t/server/realtime_conversation.h"

namespace q4t::server {
namespace {
constexpr size_t kBytes = 8ULL << 20, kChecks = 8;
// Session event IDs are checked at ingress and may use up to 256 bytes.
constexpr size_t kOverhead =
    256 + sizeof(io::Json) + sizeof(std::string) + sizeof(size_t);
ObjectReply Unavailable() {
  return object_detail::Error(503, "responses_validation_failed",
                              "Responses settings validator unavailable");
}
}  // namespace
LiveResponsesValidation::LiveResponsesValidation(LiveResponsesCheck check)
    : check_(std::move(check)) {}
LiveResponsesValidation::~LiveResponsesValidation() {
  Close();
  Join();
}
ObjectReply LiveResponsesValidation::CheckCapacity(const io::Json& settings,
                                                   size_t* bytes) const {
  if (!bytes || !check_ || stop_.stop_requested()) return Unavailable();
  if (queue_.size() >= kChecks || kOverhead > kBytes - bytes_ ||
      !MeasureRealtimeStorage(settings, kBytes - bytes_ - kOverhead, bytes))
    return object_detail::Error(429, "live_validation_capacity",
                                "Live settings validation queue is full");
  *bytes += kOverhead;
  return {};
}
void LiveResponsesValidation::Publish(io::Json settings, std::string event,
                                      size_t bytes) {
  queue_.push_back({std::move(settings), std::move(event), bytes});
  bytes_ += bytes;
  ready_ = false;
}
std::optional<LiveResponsesValidation::Result> LiveResponsesValidation::Poll() {
  if (queue_.empty()) return {};
  auto& front = queue_.front();
  ObjectReply reply;
  try {
    if (!worker_.valid()) {
      worker_ = std::async(std::launch::async, check_,
                           std::move(front.settings), stop_.get_token());
    }
    if (worker_.wait_for(std::chrono::seconds(0)) != std::future_status::ready)
      return {};
    reply = worker_.get();
  } catch (...) {
    reply = Unavailable();
  }
  const bool current = queue_.size() == 1 && !stop_.stop_requested();
  if (!reply.ok() || reply.commit_unknown || reply.http_status < 200 ||
      reply.http_status >= 300)
    reply.code = "responses_validation_failed";
  if (current) ready_ = reply.ok();
  bytes_ -= front.charge;
  auto event = std::move(front.event);
  queue_.pop_front();
  return Result{std::move(reply), std::move(event), current};
}
void LiveResponsesValidation::Close() noexcept {
  stop_.request_stop();
  ready_ = false;
  while (queue_.size() > size_t(worker_.valid())) {
    bytes_ -= queue_.back().charge;
    queue_.pop_back();
  }
}
void LiveResponsesValidation::Join() noexcept {
  Close();
  if (worker_.valid()) {
    try {
      worker_.get();
    } catch (...) {
      // Disconnected or closing. No additional events can be published.
    }
  }
  queue_.clear();
  bytes_ = 0;
}
}  // namespace q4t::server
