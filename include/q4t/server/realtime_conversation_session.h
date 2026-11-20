// One native conversation owner; model work receives immutable snapshots.
#pragma once

#include <deque>
#include <map>
#include <thread>
#include <unordered_set>

#include "q4t/server/realtime_conversation.h"
#include "q4t/server/realtime_conversation_input.h"
#include "q4t/server/realtime_prompt_settings.h"
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
  size_t SpeechBytes() const { return speech_.ChargedBytes(); }
  size_t PendingInputs() const { return input_ ? input_->PendingItems() : 0; }
  size_t InputAudioBytes() const {
    return input_ ? input_->AudioBytes() + input_->RetainedAudioBytes() : 0;
  }

 private:
  struct Work;
  ObjectReply Emit(io::Json event);
  bool Flush();
  bool Event(std::string_view type, io::Json fields);
  bool ErrorEvent(ObjectReply reply, std::string_view event_id = {});
  ObjectReply ValidateSelection(const RealtimePromptSelection& selection);
  RealtimePromptResolver PromptResolver();
  bool Settings(const io::Json& update, std::string_view event_id);
  bool CreateItem(const io::Json& event);
  bool ItemCommand(const io::Json& event, bool erase);
  bool TruncateItem(const io::Json& event);
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
  WebSocketOutput& output_;
  const RealtimeConversationCallbacks callbacks_;
  const size_t message_bytes_;
  RealtimeConversation history_;
  RealtimeSpeechHistory speech_;
  std::unique_ptr<RealtimeConversationInput> input_;
  std::optional<RealtimeSpeechOptions> locked_voice_;
  std::deque<std::unique_ptr<Work>> work_;
  std::deque<std::string> events_;
  size_t event_bytes_ = 0;
  std::chrono::steady_clock::time_point event_deadline_;
  bool started_ = false, cancelled_ = false;
};
}  // namespace q4t::server
