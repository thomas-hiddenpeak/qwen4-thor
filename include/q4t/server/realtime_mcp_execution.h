// Finite native MCP operations. Run outside the Realtime event/reader owner.
#pragma once

#include "q4t/server/realtime_mcp_catalog.h"

namespace q4t::server {
struct RealtimeMcpExecution {
  // Canonical durable native result. Project it before emitting SDK events.
  io::Json item;
  bool call_permitted = false;
  bool outcome_unknown = false;
  // Session maintenance is auxiliary to an already durable tool outcome.
  // Never turn this failure into a retry or discard that known outcome.
  bool session_saved = true;
  bool session_commit_unknown = false;
  std::string session_error;
};
// Only a fully generated public call from the connection's owned catalog may
// be proposed. No tools/call happens here. The return value is the native
// pending call or approval request; an unknown commit grants no dispatch.
ObjectReply ProposeRealtimeMcpCall(ObjectStore& objects,
                                   const ChatPlanConfig& config,
                                   const RealtimeMcpCatalog& catalog,
                                   const io::Json& generated,
                                   const std::string& response_id,
                                   const StateAccess& access);
// The session must associate the decision with its pending call. This also
// checks sealed identity and current policy before consuming approval once.
// A resumed approval explicitly transfers ownership to response_id; it does
// not replay a previously dispatched or completed call.
ObjectReply DecideRealtimeMcpCall(ObjectStore& objects,
                                  const ChatPlanConfig& config,
                                  const ResponsesMcpDefinition& definition,
                                  const io::Json& expected_call,
                                  const io::Json& decision,
                                  const std::string& response_id,
                                  const StateAccess& access);
// Requires the exact ready call owned by this process/response. Initialization
// and call use fresh deployment policy; only a successful durable Permit may
// send tools/call. Cancel/timeout after dispatch closes a bounded durable
// outcome, including unknown/no replay. Failure leaves output unchanged.
ObjectReply ExecuteRealtimeMcpCall(ObjectStore& objects,
                                   const ChatPlanConfig& config,
                                   const ResponsesMcpDefinition& definition,
                                   const io::Json& call,
                                   const std::string& response_id,
                                   const StateAccess& access,
                                   RealtimeMcpExecution* output);
}  // namespace q4t::server
