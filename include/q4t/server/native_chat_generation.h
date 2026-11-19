// Typed application execution on the ordinary Chat admission/decoder path.
#pragma once

#include <functional>
#include <utility>

#include "q4t/server/chat_response_parser.h"
#include "q4t/server/object_store.h"
#include "q4t/server/request_limits.h"
#include "q4t/server/chat_plan.h"

namespace q4t::server {
class ModelPreparation;
// Borrowed native proofs. The execution owner keeps the prepared plan and
// accounting alive through generation; no HTTP field may construct these.
struct NativeChatInputs {
  const ChatRequestPlan* plan = nullptr;
  ModelPreparation* preparation = nullptr;
};
class NativeChatGeneration {
 public:
  // The owner binds an authenticated detached finite response context and a
  // real request control. The sink accepts one whole delta or returns failure;
  // it must bound its queue and cooperate with that same cancellation owner.
  // Neither this adapter nor its caller reserves a second model/RPM lease.
  using Sink = std::function<ObjectReply(const ResponseDelta&)>;
  NativeChatGeneration(const io::Json& request, Sink sink,
                       size_t maximum_bytes = kChatBodyBytes,
                       NativeChatInputs inputs = {})
      : request_(request),
        sink_(std::move(sink)),
        maximum_bytes_(maximum_bytes),
        inputs_(inputs) {}
  const io::Json& Request() const { return request_; }
  const ChatRequestPlan* PreparedPlan() const { return inputs_.plan; }
  ModelPreparation* Preparation() const { return inputs_.preparation; }
  bool Valid() const {
    // Realtime's model step emits text/functions. Output audio and hosted MCP
    // belong to their own native owners. Do not stream private Chat web-tool
    // or moderation steps through this primary-output observer.
    return (!inputs_.preparation || inputs_.plan) && sink_ && maximum_bytes_ &&
           maximum_bytes_ <= kChatBodyBytes && request_.IsObject() &&
           !request_.Has("moderation") && !request_.Has("web_search_options") &&
           !request_.Has("modalities") && !request_.Has("audio");
  }
  Status Emit(const ResponseDelta& delta);
  void Seal() noexcept { sealed_ = true; }
  const ObjectReply& Error() const { return error_; }
  size_t AcceptedBytes() const { return accepted_bytes_; }

 private:
  const io::Json& request_;
  Sink sink_;
  const size_t maximum_bytes_;
  const NativeChatInputs inputs_;
  size_t accepted_bytes_ = 0;
  ObjectReply error_;
  bool sealed_ = false;
};
}  // namespace q4t::server
