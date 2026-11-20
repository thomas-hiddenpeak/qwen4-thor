// One bounded native transcription session, independent of socket/model type.
#pragma once

#include <atomic>
#include <deque>
#include <thread>

#include "q4t/server/asr_stream_input.h"
#include "q4t/server/realtime_audio_history.h"
#include "q4t/server/realtime_transcription_protocol.h"
#include "q4t/server/realtime_transcript_history.h"
#include "q4t/server/realtime_noise_input.h"
#include "q4t/server/realtime_semantic_turn.h"
#include "q4t/server/realtime_vad.h"
#include "q4t/server/websocket_session.h"

namespace q4t::server {
// Includes protocol/parser copies, input ring, native PCM/features and output.
// Model assets/device workspace and ingress/TLS have separate admissions.
inline constexpr size_t kRealtimeDeferredOutputBytes = 128ULL << 20;
// Original work (including live input/features), retained text/audio, output
// mailbox/deferred controls, and 256MiB for binary/Base64/JSON/frame
// temporaries. Ingress keeps its independent fixed duplex buffers. No per-item
// unlimited ownership; both retained audio and pending output charge actual
// capacity.
inline constexpr size_t kRealtimeSessionHostBytes =
    (256ULL << 20) + kRealtimeTranscriptHistoryBytes +
    kRealtimeAudioHistoryBytes + kWebSocketOutputMaximumBytes +
    kRealtimeDeferredOutputBytes + (256ULL << 20);

struct RealtimeTranscriptionJob {
  RealtimeAudioItem audio;
  RealtimeTranscriptionSettings settings;
  std::string id, previous_id;
  std::shared_ptr<AsrStreamInput> stream;
  std::shared_ptr<RealtimeAudioRecord> processed_audio;
  std::shared_ptr<RealtimeSemanticTurn> semantic;
  RealtimeTranscriptTicket transcript;
  bool deleted =
      false;  // Session owner only; worker cancellation uses control.
};
using RealtimeTranscriptSink = std::function<bool(const io::Json&)>;
struct RealtimeTranscriptionCallbacks {
  // Cooperative stop from the socket owner; checked between CPU blocks.
  std::function<bool()> interrupted;
  // Session-owner, bounded. Validate every effective setting before ack.
  std::function<ObjectReply(const RealtimeTranscriptionSettings&)> validate;
  std::function<std::shared_ptr<RequestControl>(const std::string&)> acquire;
  std::function<void(const std::shared_ptr<RequestControl>&)> release;
  // Optional fixed-frame CPU processor, prepared before socket upgrade. An
  // absent callback never acknowledges server_vad. No asset I/O in this call.
  RealtimeVad::Step vad_step;
  // True only with a connected native content classifier, not just a parser.
  bool semantic_ready = false;
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
  // Session-owner transaction for a composed conversation. Validates the
  // actual processor and pending audio before committing; emits no separate
  // session acknowledgement and never starts an ASR worker. The by-value
  // snapshot is allocated before any live pipeline mutation.
  ObjectReply Configure(RealtimeTranscriptionSettings settings);
  bool Message(WebSocketOpcode opcode, std::string_view message);
  bool Tick();
  // Session-owner only. The separate socket owner signals the pump to cancel.
  void Cancel() noexcept;
  void Join() noexcept;
  size_t QueuedItems() const { return jobs_.size(); }
  size_t AudioBytes() const { return queue_.UsedBytes() + stream_storage_; }
  size_t RetainedAudioBytes() const { return audio_history_.ChargedBytes(); }

 private:
  bool Emit(io::Json event, bool deferred = false);
  bool EmitText(std::string text, bool deferred = false);
  bool FlushVadEvents();
  bool ErrorEvent(ObjectReply reply, std::string_view event_id = {});
  bool SettingsEvent(bool legacy, bool created = false);
  bool Commit(uint64_t end_ticks, std::string_view event_id, bool automatic);
  bool CommitEvents(const RealtimeTranscriptionJob& job, bool automatic);
  bool DeleteItem(const RealtimeTranscriptionCommand& command);
  bool RetrieveItem(const RealtimeTranscriptionCommand& command);
  bool DropFrontJob();
  bool StreamMessage(const RealtimeTranscriptionCommand& command);
  bool SpeechEvent(bool started, uint64_t ticks, bool deferred);
  bool AdvanceVad();
  bool StartSemantic(uint64_t begin_ticks);
  bool FeedSemantic(uint64_t end_ticks);
  bool FinishSemantic(uint64_t end_ticks, bool automatic);
  void ResetVad();
  void ResetNoise(uint64_t ticks);
  Status ProcessedAudio(std::span<const float> samples, bool advance);
  void Release() noexcept;
  const std::string id_;
  WebSocketOutput& output_;
  const RealtimeTranscriptionCallbacks callbacks_;
  const uint64_t max_item_ticks_;
  const size_t message_bytes_;
  RealtimeTranscriptHistory history_;
  RealtimeAudioHistory audio_history_;
  RealtimeTranscriptionSettings settings_;
  RealtimeAudioQueue queue_{1};
  std::unique_ptr<RealtimeNoiseInput> noise_input_;
  std::shared_ptr<AsrStreamInput> live_input_;
  std::unique_ptr<RealtimeNoiseInput> stream_processor_;
  std::string live_id_;
  size_t stream_storage_ = 0;
  uint64_t stream_epoch_ = 0;
  bool stream_fault_ = false;
  std::unique_ptr<RealtimeVad> vad_;
  std::shared_ptr<RealtimeSemanticTurn> semantic_;
  uint64_t semantic_audio_end_ = 0;
  std::string speech_id_;
  // At most four control events, bounded by actual string capacity, including
  // a retrieved audio item. One absolute deadline is not extended by arrivals.
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
