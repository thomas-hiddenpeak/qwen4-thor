// One Live protocol owner; actual model/media work belongs to its backend.
#pragma once

#include <atomic>
#include <deque>
#include <memory>

#include "q4t/server/live_configuration.h"
#include "q4t/server/live_responses_validation.h"
#include "q4t/server/websocket_session.h"

namespace q4t::server {
using LiveEventSink = std::function<ObjectReply(io::Json)>;

class ILiveSessionBackend {
 public:
  virtual ~ILiveSessionBackend() = default;
  // Same session owner only, bounded, no blocking model execution. Failure
  // must preserve the previous configuration. Immutable jobs keep snapshots.
  virtual ObjectReply Update(const io::Json& configuration) = 0;
  // A backend that schedules automatic Responses work must use this gate for
  // every new admission. Previously admitted jobs keep their own snapshots.
  virtual void ResponsesReady(bool) noexcept {}
  virtual ObjectReply Command(const io::Json& event) = 0;
  virtual ObjectReply Tick() = 0;
  // Stop new/queued work and unresolved continuations; active work may drain.
  virtual ObjectReply Close() = 0;
  // True only after every owned task, audio tail and persistence operation
  // finishes. A signalled worker that still borrows storage is not drained.
  virtual bool Drained() const = 0;
  virtual void Cancel() noexcept = 0;
  virtual void Join() noexcept = 0;
};

struct LiveSessionCallbacks {
  LiveConfigurationContext configuration;
  // Constructs a prepared native backend, without generation or event
  // publication. Partial failure still transfers cleanup ownership via out.
  // Events may be emitted on the session thread from Command/Tick/Close,
  // never directly from a model worker or after its owning Join().
  std::function<ObjectReply(const io::Json&, const LiveEventSink&,
                            std::unique_ptr<ILiveSessionBackend>*)>
      create;
  LiveResponsesCheck validate_responses;
  std::function<bool()> interrupted;
  std::function<std::chrono::steady_clock::time_point()> now;
};

struct LiveSessionLimits {
  size_t message_bytes = kWebSocketMessageLimit;
  size_t deferred_bytes = 8ULL << 20;
  size_t deferred_events = 256;
  std::chrono::milliseconds startup{30000}, lifetime{3600000}, drain{30000},
      output{15000}, usage_interval{1000};
};

class LiveSession {
 public:
  LiveSession(std::string id, uint64_t expires_at, WebSocketOutput& output,
              LiveSessionCallbacks callbacks, LiveSessionLimits limits = {});
  ~LiveSession();
  // Prepares only the protocol owner. A primary WebSocket sends session.start
  // before the backend is created; no synthetic session.started at handshake.
  bool Start();
  bool Message(WebSocketOpcode opcode, std::string_view message);
  bool Tick();
  void Cancel() noexcept;
  void Join() noexcept;
  // The sole cross-thread observation. True means actual backend drain plus
  // session.closed queued into the output mailbox, not received by the peer.
  bool Finalized() const { return finalized_.load(); }
  size_t DeferredBytes() const { return deferred_bytes_; }

 private:
  using Clock = std::chrono::steady_clock;
  enum class State {
    kCreated,
    kWaiting,
    kActive,
    kDraining,
    kClosed,
    kCancelled
  };
  struct Pending {
    std::string text;
    size_t charge;
    Clock::time_point deadline;
    bool terminal;
  };
  bool AdvanceClock();
  bool Fail() noexcept;
  bool Flush();
  bool Emit(io::Json event, bool terminal = false,
            std::string* receipt = nullptr);
  ObjectReply BackendEvent(io::Json event);
  bool Error(ObjectReply error, std::string_view client_event = {});
  bool StartSession(const io::Json& event);
  bool UpdateSession(const io::Json& event);
  bool PublishConfiguration(io::Json response, const io::Json* settings,
                            const std::string& event, size_t charge);
  bool Dispatch(const io::Json& event);
  bool FlushResponsesCommands(bool valid);
  bool BeginClose(std::string reason, std::string client_event);
  bool Usage(bool terminal);
  double Seconds() const;
  const std::string id_;
  const uint64_t expires_at_;
  WebSocketOutput& output_;
  const LiveSessionCallbacks callbacks_;
  const LiveSessionLimits limits_;
  std::unique_ptr<ILiveSessionBackend> backend_;
  LiveResponsesValidation validation_;
  io::Json configuration_;
  std::deque<Pending> pending_;
  std::deque<io::Json> responses_commands_;
  size_t responses_command_bytes_ = 0;
  size_t deferred_bytes_ = 0;
  uint64_t sequence_ = 0;
  Clock::time_point now_, born_, started_, closing_, last_usage_;
  std::string close_reason_, close_event_;
  State state_ = State::kCreated;
  bool backend_cancelled_ = false, joined_ = false, backend_fault_ = false;
  std::atomic<bool> finalized_{false};
};
}  // namespace q4t::server
