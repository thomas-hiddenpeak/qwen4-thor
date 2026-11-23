#include "q4t/server/live_sideband.h"

#include <algorithm>

#include "object_protocol.h"
#include "q4t/io/base64.h"

namespace q4t::server {
namespace {
using namespace object_detail;
constexpr auto kReplay = std::chrono::seconds(3);
constexpr size_t kEvents = 2048, kCommands = 8;
Json Milliseconds(uint64_t ticks) {
  Json value;
  value.type = Json::Type::kNumber;
  value.number = static_cast<double>(ticks) / 48.0;
  return value;
}
}  // namespace
void LiveSideband::DropFront() {
  bytes_ -= events_.front().text->size();
  events_.pop_front();
}
void LiveSideband::Expire(Clock::time_point now) {
  while (!events_.empty() && now - events_.front().at > kReplay) DropFront();
}
void LiveSideband::Lost(Clock::time_point now, bool session_changed) noexcept {
  std::lock_guard lock(mutex_);
  if (finished_) return;
  events_.clear();
  if (session_changed) realtime_session_.reset();
  bytes_ = 0;
  ++sequence_;
  incomplete_until_ = now + kReplay;
}
void LiveSideband::Publish(std::string_view text,
                           Clock::time_point now,
                           std::string_view realtime_session) noexcept {
  if (text.empty() || text.size() > kReplayBytes ||
      realtime_session.size() > kWebSocketMessageLimit) {
    Lost(now, !realtime_session.empty());
    return;
  }
  try {
    auto copy = std::make_shared<const std::string>(text);
    auto snapshot = realtime_session.empty()
                        ? nullptr
                        : std::make_shared<const std::string>(realtime_session);
    std::lock_guard lock(mutex_);
    if (finished_) return;
    Expire(now);
    while (events_.size() >= kEvents || text.size() > kReplayBytes - bytes_) {
      incomplete_until_ =
          std::max(incomplete_until_, events_.front().at + kReplay);
      DropFront();
    }
    events_.push_back({sequence_, now, std::move(copy)});
    if (snapshot) realtime_session_ = std::move(snapshot);
    ++sequence_;
    bytes_ += text.size();
  } catch (...) {
    Lost(now, !realtime_session.empty());
  }
}
void LiveSideband::OutputAudio(uint64_t begin_ticks,
                               std::string_view pcm) noexcept {
  const auto now = Clock::now();
  try {
    std::string encoded;
    if (pcm.empty() || pcm.size() % 2 ||
        !io::EncodeBase64(pcm, kReplayBytes, &encoded)) {
      Lost(now);
      return;
    }
    // PCM16 at 24 kHz contributes one byte per 48 kHz session tick.
    Publish(OutputJson(
                Object({{"type", String("session.output_audio.delta")},
                        {"delta", String(std::move(encoded))},
                        {"start_ms", Milliseconds(begin_ticks)},
                        {"end_ms", Milliseconds(begin_ticks + pcm.size())}})),
            now);
  } catch (...) {
    Lost(now);
  }
}
void LiveSideband::InputAudio(std::string_view base64) noexcept {
  const auto now = Clock::now();
  try {
    Publish(OutputJson(Object({{"type", String("session.input_audio.append")},
                               {"audio", String(std::string(base64))}})),
            now);
  } catch (...) {
    Lost(now);
  }
}
ObjectReply LiveSideband::Attach(uint64_t* next, Clock::time_point now) {
  std::lock_guard lock(mutex_);
  if (!next || finished_)
    return Error(404, "object_not_found", "Live session not found");
  Expire(now);
  if (now <= incomplete_until_)
    return Error(503, "live_replay_unavailable",
                 "Complete Live replay window unavailable");
  *next = events_.empty() ? sequence_ : events_.front().sequence;
  return {};
}
ObjectReply LiveSideband::AttachRealtime(
    uint64_t* next, std::shared_ptr<const std::string>* session) {
  std::lock_guard lock(mutex_);
  if (finished_) return Error(404, "object_not_found", "Call not found");
  if (!next || !session || !realtime_session_)
    return Error(503, "realtime_session_unavailable",
                 "Current call settings unavailable");
  *session = realtime_session_;
  *next = sequence_;
  return {};
}
LiveSideband::Read LiveSideband::Next(
    uint64_t next, std::shared_ptr<const std::string>* text) {
  std::lock_guard lock(mutex_);
  if (!text || next > sequence_ || (finished_ && !graceful_))
    return Read::kLost;
  if (next == sequence_) return finished_ ? Read::kCompleted : Read::kPending;
  if (events_.empty() || next < events_.front().sequence) return Read::kLost;
  *text = events_[next - events_.front().sequence].text;
  return Read::kEvent;
}
void LiveSideband::Finish(bool graceful) noexcept {
  std::lock_guard lock(mutex_);
  if (finished_) return;
  finished_ = true;
  graceful_ = graceful;
  commands_.clear();
  command_bytes_ = 0;
}
bool LiveSideband::HasSpace(size_t size) const {
  std::lock_guard lock(mutex_);
  return !finished_ && size <= kCommandBytes - command_bytes_ &&
         commands_.size() < kCommands;
}
bool LiveSideband::Enqueue(WebSocketOpcode opcode, std::string_view message) {
  std::lock_guard lock(mutex_);
  if (finished_ || message.size() > kCommandBytes - command_bytes_ ||
      commands_.size() >= kCommands)
    return false;
  commands_.push_back({opcode, std::string(message)});
  command_bytes_ += message.size();
  return true;
}
std::optional<LiveSideband::Command> LiveSideband::Pop() {
  std::lock_guard lock(mutex_);
  if (commands_.empty()) return {};
  auto next = std::move(commands_.front());
  commands_.pop_front();
  command_bytes_ -= next.text.size();
  return next;
}
}  // namespace q4t::server
