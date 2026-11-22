// Concrete native Live frontend and client delegation backend.
#pragma once

#include "q4t/server/live_client_context.h"
#include "q4t/server/live_recording_store.h"
#include "q4t/server/live_decision_delegate.h"
#include "q4t/server/live_responses_delegate.h"
#include "q4t/server/realtime_chat_plan.h"

namespace q4t::server {
struct NativeLivePlan {
  io::Json settings, input;
  ChatPlanConfig decision_catalog;
  RealtimeAudioFormat format;
};
Status MapLiveStartupHistory(const io::Json& input, io::Json* output,
                             ChatPlanError* error);
// Input is the resolved Live configuration. Required yet unconnected native
// processors fail preparation; their remaining implementation is tracked in
// the Live plan, never certified by accepting a wire shape.
Status PlanNativeLiveClient(const io::Json& configuration,
                            const ChatPlanConfig& planning,
                            NativeLivePlan* output, ChatPlanError* error,
                            bool responses_connected = false,
                            bool decisions_connected = false,
                            bool recording_connected = false,
                            LiveTransport transport = LiveTransport::kWebSocket);

class NativeLiveClientBackend final : public ILiveSessionBackend {
 public:
  NativeLiveClientBackend(std::string id, LiveFrontendNative native,
                          LiveClientContextCallbacks context,
                          uint32_t max_audio_seconds,
                          size_t message_bytes = kWebSocketMessageLimit,
                          LiveResponsesNative responses = {},
                          LiveDecisionNative decisions = {},
                          std::unique_ptr<LiveRecordingCapture> recording = {});
  ~NativeLiveClientBackend() override;
  ObjectReply Prepare(io::Json configuration, NativeLivePlan plan);
  void ResponsesReady(bool ready) noexcept override;
  ObjectReply Update(const io::Json& configuration) override;
  ObjectReply Command(const io::Json& event) override;
  ObjectReply Tick() override;
  ObjectReply Close() override;
  bool Drained() const override;
  void Cancel() noexcept override;
  void Join() noexcept override;

 private:
  LiveFrontendNative Bind(LiveFrontendNative native);
  const std::string id_;
  const LiveClientContextCallbacks context_callbacks_;
  LiveResponsesNative responses_native_;
  LiveDecisionNative decisions_native_;
  std::unique_ptr<LiveRecordingCapture> recording_;
  bool MediaDrained() const;
  const size_t message_bytes_;
  LiveFrontend frontend_;
  LiveDecisionItems decision_items_;
  std::unique_ptr<LiveResponsesDelegate> responses_;
  std::unique_ptr<LiveDecisionDelegate> decisions_;
  std::unique_ptr<LiveClientContext> context_;
  io::Json configuration_;
  bool attempted_ = false, prepared_ = false, closing_ = false;
  bool cancelled_ = false, failed_ = false;
};
}  // namespace q4t::server
