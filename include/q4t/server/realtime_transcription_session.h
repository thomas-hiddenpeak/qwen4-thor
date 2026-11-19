// One bounded native transcription session, independent of socket/model type.
#pragma once

#include <atomic>
#include <deque>
#include <thread>

#include "q4t/server/realtime_transcription_protocol.h"
#include "q4t/server/realtime_vad.h"
#include "q4t/server/websocket_session.h"

namespace q4t::server {
// Includes protocol/parser copies, input ring, native PCM/features and output.
// Model assets/device workspace and ingress/TLS have separate admissions.
inline constexpr size_t kRealtimeSessionHostBytes = 256ULL << 20;

struct RealtimeTranscriptionJob {
  RealtimeAudioItem audio;
  RealtimeTranscriptionSettings settings;
  std::string id, previous_id;
};
using RealtimeTranscriptSink = std::function<bool(const io::Json&)>;
struct RealtimeTranscriptionCallbacks {
  // Socket-thread, bounded. Validate every effective setting before ack.
  std::function<ObjectReply(const RealtimeTranscriptionSettings&)> validate;
  std::function<std::shared_ptr<RequestControl>(const std::string&)> acquire;
  std::function<void(const std::shared_ptr<RequestControl>&)> release;
  // Optional fixed-frame CPU processor, prepared before socket upgrade. An
  // absent callback never acknowledges server_vad. No asset I/O in this call.
  RealtimeVad::Step vad_step;
  // Worker-thread, synchronous and cooperative. Actual per-turn admission,
  // accounting and cleanup precede returning text/logprobs/usage in value.
  std::function<ObjectReply(const RealtimeTranscriptionJob&,
                            const std::array<std::span<const uint8_t>, 2>&,
                            const std::shared_ptr<RequestControl>&,
                            const RealtimeTranscriptSink&)>
      execute;
};

class RealtimeTranscriptionSession {
 public:
  RealtimeTranscriptionSession(std::string id, WebSocketOutput& output,
                               RealtimeTranscriptionCallbacks callbacks,
                               uint32_t max_audio_seconds,
                               size_t message_bytes = kWebSocketMessageLimit);
  ~RealtimeTranscriptionSession();
  bool Start();
  bool Message(WebSocketOpcode opcode, std::string_view message);
  bool Tick();
  // Socket-thread only. No join here: the socket can still send its close.
  void Cancel() noexcept;
  void Join() noexcept;
  size_t QueuedItems() const { return queue_.Items(); }
  size_t AudioBytes() const { return queue_.UsedBytes(); }

 private:
  bool Emit(io::Json event, bool deferred = false);
  bool FlushVadEvents();
  bool ErrorEvent(ObjectReply reply, std::string_view event_id = {});
  bool SettingsEvent(bool legacy, bool created = false);
  bool Commit(uint64_t end_ticks, std::string_view event_id, bool automatic);
  bool SpeechEvent(bool started, uint64_t ticks, bool deferred);
  bool AdvanceVad();
  void ResetVad();
  void Release() noexcept;
  const std::string id_;
  WebSocketOutput& output_;
  const RealtimeTranscriptionCallbacks callbacks_;
  const uint64_t max_item_ticks_;
  const size_t message_bytes_;
  RealtimeTranscriptionSettings settings_;
  RealtimeAudioQueue queue_{1};
  std::unique_ptr<RealtimeVad> vad_;
  std::string speech_id_;
  // At most one VAD frame's control events, plus bounded later socket replies.
  // Do not start the corresponding ASR worker before committed is queued.
  std::deque<std::string> vad_events_;
  size_t vad_event_bytes_ = 0;
  std::chrono::steady_clock::time_point vad_event_deadline_;
  std::deque<RealtimeTranscriptionJob> jobs_;
  std::string previous_id_, terminal_;
  std::shared_ptr<RequestControl> control_;
  std::thread worker_;
  std::atomic<bool> done_{false};
  ObjectReply result_;
  std::chrono::steady_clock::time_point terminal_deadline_;
  bool started_ = false, cancelled_ = false;
};
}  // namespace q4t::server
