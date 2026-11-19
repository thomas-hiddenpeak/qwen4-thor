// One native conversation owner; model work receives immutable snapshots.
#pragma once

#include <deque>
#include <map>
#include <thread>
#include <unordered_set>

#include "q4t/server/realtime_conversation.h"
#include "q4t/server/realtime_response_output.h"
#include "q4t/server/websocket_session.h"

namespace q4t::server {
// Four bounded input/settings snapshots, retained history, delta mailboxes,
// deferred events and JSON/frame transaction copies. Model/media workspaces
// retain their own existing admissions; this is protocol storage only.
inline constexpr size_t kRealtimeConversationHostBytes = 512ULL << 20;
struct RealtimeConversationJob {
  std::string response_id;
  io::Json settings, response;
  RealtimeConversationSnapshot snapshot;
  bool in_conversation = true;
};
struct RealtimeGenerationResult {
  ObjectReply reply;
  io::Json message;
  RealtimeResponseEnd::State state = RealtimeResponseEnd::State::kCompleted;
  std::string reason;
  std::optional<RealtimeResponseUsage> usage;
};
using RealtimeDeltaSink = std::function<ObjectReply(const ResponseDelta&)>;
struct RealtimeConversationCallbacks {
  std::function<bool()> interrupted;
  // Session-thread, bounded/pure validation. Do not acknowledge settings that
  // the connected execution owner cannot honor. I/O belongs to execute.
  std::function<ObjectReply(const io::Json&)> validate;
  std::function<ObjectReply(const RealtimeConversationJob&)> prepare;
  std::function<std::shared_ptr<RequestControl>(const std::string&)> acquire;
  std::function<void(const std::shared_ptr<RequestControl>&)> release;
  // Cooperative worker. Normal client admission/quota/model accounting is
  // owned here; no secondary model or RPM reservation in the session.
  std::function<RealtimeGenerationResult(const RealtimeConversationJob&,
                                         const std::shared_ptr<RequestControl>&,
                                         const RealtimeDeltaSink&)>
      execute;
};

class RealtimeConversationSession {
 public:
  RealtimeConversationSession(std::string id, std::string conversation_id,
                              io::Json settings, WebSocketOutput& output,
                              RealtimeConversationCallbacks callbacks,
                              size_t message_bytes = kWebSocketMessageLimit);
  ~RealtimeConversationSession();
  bool Start();
  bool Message(WebSocketOpcode opcode, std::string_view message);
  bool Tick();
  // All methods belong to the session owner, never the socket reader. The
  // socket pump signals its own stop flag, then joins the session worker.
  void Cancel() noexcept;
  void Join() noexcept;
  size_t ActiveResponses() const { return work_.size(); }
  size_t HistoryBytes() const { return history_.ChargedBytes(); }

 private:
  struct Work;
  ObjectReply Emit(io::Json event);
  bool Flush();
  bool Event(std::string_view type, io::Json fields);
  bool ErrorEvent(ObjectReply reply, std::string_view event_id = {});
  bool Settings(const io::Json& update, std::string_view event_id);
  bool CreateItem(const io::Json& event);
  bool ItemCommand(const io::Json& event, bool erase);
  bool CreateResponse(const io::Json& event);
  bool CancelResponse(const io::Json& event);
  ObjectReply Store(Work& work, const io::Json& item, bool begin,
                    bool terminal);
  void Release(Work& work) noexcept;
  const std::string id_, conversation_id_;
  io::Json settings_;
  WebSocketOutput& output_;
  const RealtimeConversationCallbacks callbacks_;
  const size_t message_bytes_;
  RealtimeConversation history_;
  std::deque<std::unique_ptr<Work>> work_;
  std::deque<std::string> events_;
  size_t event_bytes_ = 0;
  std::chrono::steady_clock::time_point event_deadline_;
  bool started_ = false, cancelled_ = false;
};
}  // namespace q4t::server
