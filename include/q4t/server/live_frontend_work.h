// One actual Live frontend generation with a bounded, cancellable mailbox.
#pragma once

#include <condition_variable>
#include <variant>

#include "q4t/server/live_audio_output.h"
#include "q4t/server/realtime_conversation_session.h"

namespace q4t::server {
struct LiveFrontendCallbacks {
  std::function<ObjectReply(const RealtimeConversationJob&)> prepare;
  std::function<std::shared_ptr<RequestControl>(const std::string&)> acquire;
  std::function<void(const std::shared_ptr<RequestControl>&)> release;
  std::function<RealtimeGenerationResult(
      const RealtimeConversationJob&, const std::shared_ptr<RequestControl>&,
      const RealtimeDeltaSink&, const RealtimeSpeechSink&)>
      execute;
  // Same-owner history mutation and optional retained native PCM. Neither is
  // a worker callback; live borrowers outlast cancel through the actual join.
  RealtimeResponseOutput::ItemSink item;
  RealtimeResponseOutput::AudioSink audio;
  LiveEventSink emit;
  std::function<uint64_t()> timeline;
};

class LiveFrontendWork {
 public:
  static constexpr size_t kMailboxBytes = 8ULL << 20, kMailboxItems = 256;
  LiveFrontendWork(RealtimeConversationJob job, std::string conversation_id,
                   RealtimeAudioFormat format, LiveFrontendCallbacks callbacks,
                   std::string client_event = {});
  ~LiveFrontendWork();
  ObjectReply Start();
  ObjectReply Tick();
  void Cancel() noexcept;
  void Join() noexcept;
  bool Finished() const { return finished_; }
  // Result is readable only after Finished(); the model worker owns it earlier.
  const RealtimeGenerationResult& Result() const { return result_; }
  io::Json Output() const { return output_.Output(); }
  uint64_t AudioEndTicks() const { return audio_ ? audio_->EndTicks() : 0; }
  size_t QueuedBytes() const;

 private:
  struct Queued {
    std::variant<ResponseDelta, RealtimeSpeechEvent> value;
    size_t charge;
  };
  ObjectReply Push(const ResponseDelta& delta);
  ObjectReply Push(const RealtimeSpeechEvent& event);
  ObjectReply Enqueue(Queued queued);
  ObjectReply Deliver(Queued& queued);
  void Run() noexcept;
  void Release() noexcept;
  const RealtimeConversationJob job_;
  const RealtimeAudioFormat format_;
  const LiveFrontendCallbacks callbacks_;
  const std::string client_event_;
  RealtimeResponseOutput output_;
  std::unique_ptr<LiveAudioOutput> audio_;
  std::shared_ptr<RequestControl> control_;
  std::thread worker_;
  mutable std::mutex mutex_;
  std::condition_variable changed_;
  std::deque<Queued> queue_;
  size_t bytes_ = 0, count_ = 0;
  std::atomic<bool> done_{false};
  RealtimeGenerationResult result_;
  ObjectReply delivery_error_;
  bool attempted_ = false, started_ = false, cancelled_ = false,
       finished_ = false;
};
}  // namespace q4t::server
