#pragma once

#include <functional>

#include "q4t/server/chat_plan.h"
#include "q4t/server/http_response.h"
#include "q4t/server/model_preparation.h"
#include "q4t/server/request_control.h"

namespace q4t::server {
// Server-created typed work, never an HTTP route or serialized loopback call.
// Borrowed owners outlive the call; no independent client/request/RPM lease.
struct InternalModelStep {
  const io::Json& request;
  ChatPlanConfig config;
  ModelPreparation& accounting;
  std::function<ObjectReply()> before_model;
  ObjectReply error;
  bool completed = false;
  std::string content, finish_reason;
  uint64_t input_tokens = 0, output_tokens = 0;
};
using InternalModelExecutor = std::function<void(
    detail::ResponseState*, std::shared_ptr<RequestControl>)>;
ObjectReply ExecuteInternalModelStep(int fd, InternalModelStep* step,
                                     std::shared_ptr<RequestControl> control,
                                     const InternalModelExecutor& execute);
}  // namespace q4t::server
