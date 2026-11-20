// Session-thread MCP execution lifecycle, separate from completed generation.
#pragma once

#include <deque>
#include <memory>

#include "q4t/server/realtime_conversation.h"
#include "q4t/server/realtime_mcp_execution.h"
#include "q4t/server/realtime_response_output.h"

namespace q4t::server {
enum class RealtimeMcpAction { kPropose, kDecide, kExecute, kAbandon };
struct RealtimeMcpCallJob {
  RealtimeMcpAction action = RealtimeMcpAction::kPropose;
  std::string response_id;
  // Private immutable snapshot selected for actual model generation.
  RealtimeMcpCatalog catalog;
  io::Json generated, native_call, decision;
};
struct RealtimeMcpCallCallbacks {
  // The native handler binds fresh authenticated scope/deployment. Finite
  // worker operation; the session never performs storage/network on its owner.
  // Abandon runs only after the earlier worker is joined and uses the supplied
  // independent cleanup deadline, not the disconnected client's cancellation.
  std::function<ObjectReply(
      const RealtimeMcpCallJob&, std::chrono::steady_clock::time_point,
      const std::function<bool()>&, RealtimeMcpExecution*)>
      run;
  std::function<ObjectReply(const io::Json&)> emit;
  std::function<std::string(std::string_view)> new_id;
  std::function<bool()> interrupted;
};
struct RealtimeMcpCallLimits {
  size_t workers = 1;
  size_t entries = 64;
  size_t retained_bytes = 64ULL << 20;
  std::chrono::milliseconds timeout{30000};
  std::chrono::milliseconds approval_timeout{300000};
};

class RealtimeMcpCalls {
 public:
  RealtimeMcpCalls(RealtimeConversation& history,
                   RealtimeMcpCallCallbacks callbacks,
                   RealtimeMcpCallLimits limits = {});
  ~RealtimeMcpCalls();
  RealtimeMcpCalls(const RealtimeMcpCalls&) = delete;
  RealtimeMcpCalls& operator=(const RealtimeMcpCalls&) = delete;
  // Transfers only the exact pending server item after response.done. Never
  // accepts a client-created item, stale ticket, duplicate or another target.
  ObjectReply Begin(const RealtimeItemTicket& ticket,
                    const RealtimeMcpGeneratedCall& generated,
                    const RealtimeMcpCatalog& catalog, std::string response_id,
                    bool in_conversation);
  // Validates and reserves the user's decision before its finite native CAS.
  // A repeated or forged decision cannot enqueue another dispatch.
  ObjectReply Decide(const io::Json& item,
                     std::optional<std::string_view> previous = {});
  ObjectReply Tick();
  // Revokes future dispatch immediately. An active worker retains its slot
  // until joined; its known result is preserved and never applied to a reused
  // history ID. Cleanup cannot cancel a different resumed execution owner.
  ObjectReply Erase(const RealtimeItemTicket& ticket);
  bool Owns(const RealtimeItemTicket& ticket) const;
  bool CancelResponse(std::string_view response_id);
  void Cancel() noexcept;
  void Join() noexcept;
  size_t Pending() const;
  size_t Workers() const;
  size_t RetainedBytes() const { return retained_bytes_; }
  size_t CleanupFailures() const { return cleanup_failures_; }

 private:
  struct Call;
  ObjectReply Event(std::string_view type, io::Json fields);
  ObjectReply Finish(Call& call);
  ObjectReply Complete(Call& call, const io::Json& native);
  ObjectReply Fail(Call& call, std::string_view message);
  void Start(Call& call);
  void Abandon(Call& call);
  bool UpdateCharge(Call& call);
  RealtimeConversation& history_;
  const RealtimeMcpCallCallbacks callbacks_;
  const RealtimeMcpCallLimits limits_;
  std::deque<std::unique_ptr<Call>> calls_;
  size_t retained_bytes_ = 0;
  size_t cleanup_failures_ = 0;
  bool valid_ = false, cancelled_ = false, broken_ = false;
};
}  // namespace q4t::server
