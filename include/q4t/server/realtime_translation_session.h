#pragma once

#include "q4t/server/realtime_noise_input.h"
#include "q4t/server/realtime_session_pump.h"
#include "q4t/server/realtime_translation.h"
#include "q4t/server/realtime_vad.h"
#include "q4t/server/websocket_session.h"

namespace q4t::server {
using TranslationEventSink = std::function<bool(io::Json)>;

struct RealtimeTranslationNative {
  RealtimeVad::Step vad;
  std::function<std::shared_ptr<RequestControl>(const std::string&)> acquire;
  std::function<void(const std::shared_ptr<RequestControl>&)> release;
  std::function<ObjectReply(
      std::span<const float>, const RealtimeTranslationSettings&,
      const std::shared_ptr<RequestControl>&, const TranslationEventSink&)>
      execute;
};

// The pump's worker owns PCM, settings and output order. Only Interrupt and
// Completed cross threads. No socket thread waits for model execution.
class RealtimeTranslationSession {
 public:
  RealtimeTranslationSession(std::string id, TranslationEventSink output,
                             RealtimeTranslationNative native,
                             size_t message_bytes, uint32_t max_audio_seconds);
  bool Start();
  ObjectReply StartConfigured(const io::Json& startup);
  bool Message(WebSocketOpcode opcode, std::string_view message);
  bool Tick() const { return !interrupted_.load(); }
  void Interrupt();
  void Cancel() { Interrupt(); }
  void Join() {}  // All model calls execute synchronously on the pump worker.
  bool Failed() const { return failed_.load(); }
  bool Completed() const { return completed_.load(); }

 private:
  void ResetProcessor(uint64_t origin);
  ObjectReply FinishInput();
  Status Collect(std::span<const float> samples);
  Status Frame(std::span<const float> frame);
  ObjectReply Translate();
  bool Emit(io::Json event);
  bool Fail(const ObjectReply& reply, const std::string& client_event);
  bool Report(const ObjectReply& reply, const std::string& client_event);

  const std::string id_;
  const TranslationEventSink output_;
  const RealtimeTranslationNative native_;
  const size_t message_bytes_, max_samples_;
  RealtimeTranslationSettings settings_;
  int64_t expires_ = 0;
  std::unique_ptr<RealtimeNoiseInput> processor_;
  audio::VadState vad_;
  std::array<float, 512> frame_{};
  size_t filled_ = 0, quiet_samples_ = 0;
  bool voiced_ = false;
  std::vector<float> segment_;
  ObjectReply failure_;
  std::mutex mutex_;
  std::shared_ptr<RequestControl> active_;
  std::atomic<bool> interrupted_{false}, completed_{false}, failed_{false};
};

class RealtimeTranslationPump final : public RealtimeSessionPump {
 public:
  RealtimeTranslationPump(std::shared_ptr<RealtimeTranslationSession> session,
                          size_t bytes)
      : RealtimeSessionPump(bytes) {
    Attach(std::move(session));
  }
};
}  // namespace q4t::server
