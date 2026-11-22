// Invoke the ordinary online Responses owner for a single Live delegation.
#pragma once

#include "q4t/server/actual_usage.h"
#include "q4t/server/client_policy.h"
#include "q4t/server/http_request.h"
#include "q4t/server/native_responses_stream.h"
#include "q4t/server/request_control.h"

namespace q4t::server {
struct LiveResponsesResult {
  ObjectReply reply;
  io::Json terminal;
  std::optional<ActualUsage> usage;
};
LiveResponsesResult ExecuteLiveResponses(
    const io::Json& settings, io::Json history, const ClientIdentity& client,
    ClientLease& permit, const std::shared_ptr<RequestControl>& control,
    HttpBodyBudget& input_budget, NativeResponsesStream::Sink sink,
    const std::function<void(const std::string&)>& execute);
}  // namespace q4t::server
