// One Live evaluation uses the ordinary online Decisions handler.
#pragma once

#include "q4t/server/client_policy.h"
#include "q4t/server/decision_plan.h"
#include "q4t/server/http_request.h"
#include "q4t/server/object_store.h"
#include "q4t/server/request_control.h"

namespace q4t::server {
// Local item IDs select stored inputs and have no meaning to the REST API.
// Only the outer item metadata is removed. Embedded JSON and call IDs survive.
Status MakeLiveDecisionRequest(std::string model, const io::Json& questions,
                               io::Json conversation, io::Json items,
                               const ChatPlanConfig& planning, io::Json* output,
                               ChatPlanError* error);
ObjectReply ExecuteLiveDecision(
    const io::Json& request, const ClientIdentity& client, ClientLease& permit,
    const std::shared_ptr<RequestControl>& control,
    HttpBodyBudget& input_budget,
    const std::function<void(const std::string&)>& execute);
}  // namespace q4t::server
