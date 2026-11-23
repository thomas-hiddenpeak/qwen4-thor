// One native conversation owner; model work receives immutable snapshots.
#pragma once

#include "q4t/server/realtime_shape.h"

#include <deque>
#include <map>
#include <thread>
#include <unordered_set>

#include "q4t/server/realtime_conversation.h"
#include "q4t/server/realtime_trace_session.h"
#include "q4t/server/realtime_conversation_input.h"
#include "q4t/server/realtime_prompt_settings.h"
#include "q4t/server/realtime_mcp_context.h"
#include "q4t/server/realtime_mcp_imports.h"
#include "q4t/server/realtime_mcp_calls.h"
#include "q4t/server/realtime_response_output.h"
#include "q4t/server/realtime_speech_execution.h"
#include "q4t/server/realtime_speech_history.h"
#include "q4t/server/websocket_session.h"

namespace q4t::server {
// Four workers: at most 384MiB each for snapshots, normalization/context-cut
// transaction copies, finite output and framing; 512MiB for the session's
// retained history, parser, mailboxes and deferred events. Each executable plan
// separately caps its retained JSON at 64MiB. Model/media workspaces retain
// their existing admissions; this reservation is protocol storage only.
inline constexpr size_t kRealtimeConversationHostBytes = 2ULL << 30;
struct RealtimeConversationJob {
  std::string response_id;
  io::Json settings, response;
  RealtimeConversationSnapshot snapshot;
  bool in_conversation = true;
  std::optional<PromptSnapshot> prompt = {};
  // Private proof from the connected session owner, never a JSON option.
  bool native_input = false;
  std::shared_ptr<const RealtimeMcpContext> mcp = {};
  bool mcp_definitions_only = false;
};
struct RealtimeGenerationResult {
  ObjectReply reply;
  io::Json message;
  RealtimeResponseEnd::State state = RealtimeResponseEnd::State::kCompleted;
  std::string reason;
  std::optional<RealtimeResponseUsage> usage;
  // Only an actually completed default-context generation may publish a cut.
  std::optional<std::vector<size_t>> retained_input_indices;
};
using RealtimeDeltaSink = std::function<ObjectReply(const ResponseDelta&)>;
struct RealtimeConversationCallbacks {
  std::function<bool()> interrupted;
  // Session-thread, bounded/pure validation. Do not acknowledge settings that
  // the connected execution owner cannot honor.
  std::function<ObjectReply(const io::Json&)> validate;
  std::function<ObjectReply(const RealtimeConversationJob&)> prepare;
  RealtimePromptValidator validate_prompt;
  // A separate bounded lookup. The session supplies the pump's stop signal;
  // implementations must propagate it and a finite deadline to object I/O.
  std::function<ObjectReply(const io::Json&, size_t,
                            const std::function<bool()>&, PromptSnapshot*)>
      resolve_prompt;
  std::function<std::shared_ptr<RequestControl>(const std::string&)> acquire;
  std::function<void(const std::shared_ptr<RequestControl>&)> release;
  // Cooperative worker. Normal client admission/quota/model accounting is
  // owned here; no secondary model or RPM reservation in the session.
  std::function<RealtimeGenerationResult(
      const RealtimeConversationJob&, const std::shared_ptr<RequestControl>&,
      const RealtimeDeltaSink&, const RealtimeSpeechSink&)>
      execute;
  // Connected once by the actual native handler; no auxiliary socket. The
  // handler separately reserves kRealtimeConversationInputHostBytes.
  std::optional<RealtimeTranscriptionCallbacks> input;
  uint32_t max_audio_seconds = 600;
  // Injectable monotonic clock for bounded protocol timers; no wire override.
  std::function<std::chrono::steady_clock::time_point()> now;
  // Both native adapters are required before MCP settings can be accepted.
  // The session binds publication and IDs; worker callbacks bind fresh scope.
  std::optional<RealtimeMcpImportCallbacks> mcp_import = {};
  std::optional<RealtimeMcpCallCallbacks> mcp_call = {};
  // Trusted native observer binding; never sourced from JSON metadata/user.
  std::function<RealtimeTraceStore*()> traces = {};
  std::string trace_scope = {};
  // The media owner cuts queued playout only when VAD actually interrupts.
  std::function<void()> interrupt_playout = {};
};

class RealtimeConversationSession {
 public:
  RealtimeConversationSession(std::string id, std::string conversation_id,
                              io::Json settings, WebSocketOutput& output,
                              RealtimeConversationCallbacks callbacks,
                              size_t message_bytes = kWebSocketMessageLimit);
  RealtimeConversationSession(std::string id, std::string conversation_id,
                              io::Json settings, SessionEventOutput output,
                              RealtimeConversationCallbacks callbacks,
                              size_t message_bytes = kWebSocketMessageLimit);
  // Apply the complete media-call configuration, including a scoped prompt,
  // before publishing session.created. A failure is returned to HTTP.
  ObjectReply PreviewCredential(const io::Json& configuration, io::Json* resource);
  ObjectReply StartConfigured(const io::Json& configuration) {
    return StartCall(configuration, RealtimeShape::kSession);
  }
  ObjectReply StartCall(const io::Json& configuration,
                        RealtimeShape shape = RealtimeShape::kCallAccept);
  ~RealtimeConversationSession();
  bool Start();
  bool Message(WebSocketOpcode opcode, std::string_view message);
  bool Tick();
  // The native media clock commits mono 24 kHz samples to its paced sender.
  bool TruncatePlayout(const std::string& item_id, uint64_t submitted_samples);
  // All methods belong to the session owner, never the socket reader. The
  // socket pump signals its own stop flag, then joins the session worker.
  void Cancel() noexcept;
  void Join() noexcept;
  size_t ActiveResponses() const { return work_.size(); }
  size_t HistoryBytes() const { return history_.ChargedBytes(); }
  size_t SpeechBytes() const { return speech_.ChargedBytes(); }
  size_t PendingInputs() const { return input_ ? input_->PendingItems() : 0; }
  size_t PendingMcp() const {
    return (mcp_imports_ ? mcp_imports_->Pending() : 0) +
           (mcp_calls_ ? mcp_calls_->Pending() : 0);
  }
  size_t InputAudioBytes() const {
    return input_ ? input_->AudioBytes() + input_->RetainedAudioBytes() : 0;
  }

 private:
  struct Work;
  void ConfigureTracing() noexcept;
  ObjectReply Emit(io::Json event);
  bool Flush();
  bool Event(std::string_view type, io::Json fields);
  bool ErrorEvent(ObjectReply reply, std::string_view event_id = {});
  ObjectReply ValidateSelection(const RealtimePromptSelection& selection);
  ObjectReply SelectMcp(const RealtimePromptSelection& source,
                        const io::Json& response, bool refresh,
                        RealtimeMcpSelection* selection,
                        std::shared_ptr<const RealtimeMcpContext>* context,
                        bool* commit);
  ObjectReply CaptureMcp(const RealtimeMcpSelection& selection,
                         std::shared_ptr<const RealtimeMcpContext>* context);
  bool ImportMcp(const RealtimeMcpSelection& selection, Work* work = nullptr);
  io::Json PublicSettings() const;
  RealtimePromptResolver PromptResolver();
  ObjectReply StartSession(const io::Json* call, io::Json* preview = nullptr);
  ObjectReply ApplySettings(const io::Json& update);
  bool Settings(const io::Json& update, std::string_view event_id);
  bool CreateItem(const io::Json& event);
  bool ItemCommand(const io::Json& event, bool erase);
  bool TruncateItem(const io::Json& event);
  bool TruncateStoredAudio(const RealtimeItemTicket& ticket, uint64_t end_ms,
                           std::string_view event_id);
  ObjectReply ValidateSpeech(const RealtimeSpeechOptions& speech) const;
  bool CreateResponse(const io::Json& event,
                      RealtimeInputCommit* automatic = nullptr);
  void StartWork(Work& work);
  void AdvanceWork(Work& work);
  bool InputReady();
  bool InputSpeech(bool started, bool interrupt);
  bool CancelResponse(const io::Json& event);
  ObjectReply Store(Work& work, const io::Json& item, bool begin,
                    bool terminal);
  ObjectReply StoreAudio(Work& work, std::string_view id,
                         std::string_view bytes, RealtimeAudioCommit commit);
  void Release(Work& work) noexcept;
  const std::string id_, conversation_id_;
  RealtimePromptSettings settings_;
  const SessionEventOutput output_;
  const RealtimeConversationCallbacks callbacks_;
  const size_t message_bytes_;
  RealtimeConversation history_;
  RealtimeSpeechHistory speech_;
  std::unique_ptr<RealtimeConversationInput> input_;
  RealtimeMcpDefinitions mcp_definitions_;
  RealtimeMcpSelection mcp_selection_;
  std::shared_ptr<const RealtimeMcpContext> mcp_current_;
  std::unique_ptr<RealtimeMcpImports> mcp_imports_;
  std::unique_ptr<RealtimeMcpCalls> mcp_calls_;
  std::optional<RealtimeSpeechOptions> locked_voice_;
  std::optional<RealtimeSessionTrace> trace_;
  std::deque<std::unique_ptr<Work>> work_;
  std::deque<std::string> events_;
  size_t event_bytes_ = 0;
  std::chrono::steady_clock::time_point event_deadline_;
  bool started_ = false, cancelled_ = false;
};
}  // namespace q4t::server
