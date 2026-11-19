// One Realtime response invokes the normal authenticated Chat owner once.
#pragma once

#include "q4t/server/client_policy.h"
#include "q4t/server/native_chat_generation.h"
#include "q4t/server/realtime_conversation_session.h"
#include "q4t/server/http_request.h"

namespace q4t::server {
// Claim once before any model input-preparation step. The primary generation
// consumes this same proof exactly once, including rejected primary attempts.
class RealtimeInvocation {
 public:
  explicit RealtimeInvocation(std::shared_ptr<RequestControl> control)
      : control_(std::move(control)),
        claimed_(control_ && !control_->Cancelled() &&
                 control_->ClaimInvocation()) {}
  RealtimeInvocation(const RealtimeInvocation&) = delete;
  RealtimeInvocation& operator=(const RealtimeInvocation&) = delete;
  bool Ready(const std::shared_ptr<RequestControl>& control) const {
    return claimed_ && !consumed_ && control == control_ &&
           !control_->Cancelled();
  }
  bool Consume(const std::shared_ptr<RequestControl>& control) {
    if (!Ready(control)) return false;
    consumed_ = true;
    return true;
  }

 private:
  const std::shared_ptr<RequestControl> control_;
  const bool claimed_;
  bool consumed_ = false;
};

RealtimeGenerationResult ExecuteRealtimeChat(
    const io::Json& request, const ClientIdentity& client, ClientLease& permit,
    const std::shared_ptr<RequestControl>& control,
    const RealtimeDeltaSink& emit,
    const std::function<void(NativeChatGeneration&)>& execute,
    HttpBodyBudget& input_budget, NativeChatInputs inputs = {},
    RealtimeInvocation* invocation = nullptr);
}  // namespace q4t::server
