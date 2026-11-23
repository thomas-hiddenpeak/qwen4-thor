// One Live frontend conversation; native ASR and LLM/TTS share admission.
#pragma once

#include <map>

#include "q4t/server/live_audio_fixture.h"
#include "q4t/server/live_audio_input.h"
#include "q4t/server/live_frontend_work.h"

namespace q4t::server {
struct LiveFrontendNative {
  RealtimeTranscriptionCallbacks input;
  decltype(LiveFrontendCallbacks::prepare) prepare;
  decltype(LiveFrontendCallbacks::acquire) acquire;
  decltype(LiveFrontendCallbacks::release) release;
  decltype(LiveFrontendCallbacks::execute) execute;
  LiveEventSink emit;
  LiveRecordSink record = {};
  // Same-owner receipt, after actual worker join and request release. Tools
  // may only be dispatched from a verified complete result, never deltas.
  std::function<ObjectReply(const RealtimeConversationJob&,
                            const RealtimeGenerationResult&, const io::Json&)>
      completed;
  std::function<std::chrono::steady_clock::time_point()> now;
  std::function<bool()> fixture_ready = {};
};

class LiveFrontend {
 public:
  static constexpr size_t kQueuedTurns = 4, kSnapshotBytes = 96ULL << 20;
  LiveFrontend(std::string id, LiveFrontendNative native,
               uint32_t max_audio_seconds,
               size_t message_bytes = kWebSocketMessageLimit);
  ~LiveFrontend();
  // Internal native Realtime settings/history; the public Live adapter owns
  // token counting, Live context/delegation policy and format negotiation.
  // No generation or output during preparation. A failed instance is discarded.
  ObjectReply Prepare(io::Json settings, const io::Json& initial,
                      RealtimeAudioFormat format);
  ObjectReply Settings(io::Json settings, bool respond = false,
                       std::string client_event = {}, io::Json response = {});
  ObjectReply Create(io::Json item, bool respond = false,
                     std::string client_event = {}, io::Json response = {});
  ObjectReply Respond(std::string client_event = {}, io::Json response = {});
  // Native scheduler IDs only; do not authorize client-supplied cancellation.
  bool CancelResponse(std::string_view response_id);
  ObjectReply AppendAudio(std::string_view audio,
                          std::string_view client_event = {});
  ObjectReply Mute(bool muted, std::string_view client_event = {});
  ObjectReply PlayFixture(std::string_view audio,
                          std::string client_event = {});
  bool FixtureActive() const { return fixture_ != nullptr; }
  ObjectReply Tick(bool allow_start = true);
  ObjectReply Close();
  bool Drained() const;
  void Cancel() noexcept;
  void Join() noexcept;
  uint64_t Timeline() const;
  uint64_t RecordingEndTicks() const {
    return std::max(Timeline(), recorded_input_end_);
  }
  size_t QueuedTurns() const { return queue_.size(); }
  size_t QueuedBytes() const { return queued_bytes_; }
  Status ForkHistory(io::Json* items) const;
  size_t HistoryItems() const { return history_.Items(); }
  bool Generating() const { return work_ != nullptr; }
  bool InputExecuting() const { return input_.Executing(); }

 private:
  struct Turn {
    RealtimeConversationJob job;
    std::string client_event;
    size_t charge = 0;
  };
  LiveAudioInputCallbacks InputCallbacks();
  ObjectReply Committed(RealtimeInputCommit notice);
  ObjectReply Capture(Turn* turn, const io::Json& settings);
  ObjectReply Charge(Turn* turn);
  ObjectReply Enqueue(Turn turn);
  ObjectReply StartWork();
  ObjectReply AdvanceWork();
  ObjectReply AdvanceFixture();
  ObjectReply Item(const io::Json& item, bool begin, bool terminal);
  ObjectReply Audio(std::string_view id, std::string_view bytes,
                    RealtimeAudioCommit commit);
  ObjectReply Report(const ObjectReply& reply, std::string_view client_event);
  ObjectReply Fail(const ObjectReply& reply);
  void DropQueued() noexcept;
  bool Active() const;
  std::chrono::steady_clock::time_point Now() const;
  const std::string id_;
  const LiveFrontendNative native_;
  RealtimeConversation history_;
  LiveAudioInput input_;
  io::Json settings_;
  RealtimeAudioFormat format_;
  RealtimeSpeechOptions speech_;
  std::chrono::steady_clock::time_point started_;
  std::deque<Turn> queue_;
  size_t queued_bytes_ = 0;
  std::optional<Turn> active_;
  std::unique_ptr<LiveFrontendWork> work_;
  std::unique_ptr<LiveAudioFixture> fixture_;
  std::map<std::string, RealtimeItemTicket> outputs_;
  uint64_t WallTicks() const;
  uint64_t output_ticks_ = 0, recorded_input_end_ = 0;
  bool attempted_ = false, prepared_ = false, closing_ = false;
  bool cancelled_ = false, failed_ = false, joined_ = false;
};
}  // namespace q4t::server
