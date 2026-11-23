#pragma once

#include <functional>

#include "q4t/server/chat_plan.h"
#include "q4t/server/http_response.h"
#include "q4t/server/model_preparation.h"
#include "q4t/server/request_control.h"

namespace q4t::server {
class StructuredRequestLease;
struct DecisionScore;
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
  // A serial classifier may reuse its parent's grammar admission after that
  // parent's decoder is destroyed. This never permits overlapping decoders.
  StructuredRequestLease* structured_owner = nullptr;
  // Optional request-owned readback of the first target logit row.
  DecisionScore* decision = nullptr;
  // A hosted agent returns its model-authored assistant message, including
  // validated child calls or a refusal, to the same request's dispatcher.
  io::Json* agent_message = nullptr;
  uint64_t remaining_input = UINT64_MAX;
};
using InternalModelExecutor = std::function<void(
    detail::ResponseState*, std::shared_ptr<RequestControl>)>;
ObjectReply ExecuteInternalModelStep(int fd, InternalModelStep* step,
                                     std::shared_ptr<RequestControl> control,
                                     const InternalModelExecutor& execute);
}  // namespace q4t::server
