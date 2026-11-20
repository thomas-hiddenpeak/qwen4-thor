// Client-owned Live delegation and trusted application context transitions.
#pragma once

#include <map>
#include <set>

#include "q4t/server/live_frontend.h"

namespace q4t::server {
inline constexpr std::string_view kLiveDelegateTool = "q4t_live_delegate";
// Internal control tool, not an external task executor. Its successful output
// means a client handoff was created, never that the task itself completed.
io::Json LiveClientFrontendSettings(io::Json native_settings);

struct LiveClientContextCallbacks {
  decltype(LiveConfigurationContext::count_text) count_text;
  LiveEventSink emit;
  std::function<std::chrono::steady_clock::time_point()> now;
};
class LiveClientContext {
 public:
  static constexpr size_t kMaximumDelegations = 128;
  static constexpr size_t kInstructionsBytes = 1ULL << 20;
  LiveClientContext(std::string owner, LiveFrontend& frontend,
                    io::Json settings, LiveClientContextCallbacks callbacks);
  bool Valid() const { return valid_; }
  ObjectReply Append(const io::Json& event);
  ObjectReply Complete(const RealtimeConversationJob& job,
                       const RealtimeGenerationResult& result,
                       const io::Json& output,
                       std::string_view client_event = {});
  ObjectReply Tick();
  // Apply only to subsequent delegations; existing deadlines stay frozen.
  ObjectReply AcknowledgementDelay(uint32_t milliseconds);
  void Close() noexcept;
  bool Known(std::string_view id) const;
  size_t Delegations() const { return entries_.size(); }
  const io::Json& Settings() const { return settings_; }

 private:
  using Clock = std::chrono::steady_clock;
  struct Entry {
    std::string call_id, response_id;
    Clock::time_point due, retry, deadline;
    bool pending = true;
  };
  Clock::time_point Now() const;
  ObjectReply Emit(io::Json event);
  ObjectReply Fail(ObjectReply reply);
  ObjectReply Verify(const RealtimeConversationJob& job,
                     const RealtimeGenerationResult& result,
                     const io::Json& output) const;
  void CancelAcknowledgements();
  const std::string owner_;
  LiveFrontend& frontend_;
  io::Json settings_;
  const LiveClientContextCallbacks callbacks_;
  std::map<std::string, Entry> entries_;
  std::set<std::string> calls_;
  uint32_t delay_ms_ = 0;
  bool valid_ = false, closed_ = false, failed_ = false;
};
}  // namespace q4t::server
