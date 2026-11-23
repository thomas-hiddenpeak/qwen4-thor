// Native wire shapes derived from the pinned Managed Agents SDK.
#pragma once

#include "q4t/server/responses_state.h"

namespace q4t::server {
enum class AgentSessionShape {
  kCreate,
  kUpdate,
  kResource,
  kDeleted,
  kQuery,
  kInputEvent,
  kEvent,
  kItem,
  kTurn
};
Status ValidateAgentSession(const io::Json& value, AgentSessionShape shape,
                            ChatPlanError* error);
// Resolve an inline or owned registered agent into the immutable public session
// snapshot. Input validation uses the session schema before this step.
ObjectReply ResolveSessionAgent(ObjectStore& objects, const io::Json& request,
                                const ChatPlanConfig& config,
                                const StateAccess& access);
struct AgentSessionStart {
  io::Json session;
  io::Json input;
};

// Prepare a session that uses the native model directly (environment "none").
// The result is not published until its initial turn has a durable owner.
ObjectReply PrepareDirectAgentSession(
    ObjectStore& objects, const io::Json& request, const ChatPlanConfig& config,
    const StateAccess& access, std::string_view session_id, int64_t created,
    AgentSessionStart* start);
class ResponsesExecution;
// Prepare native inference and bind its first queue receipt to the session.
ObjectReply PrepareAgentSessionExecution(
    ObjectStore& objects, const io::Json& request, const ChatPlanConfig& config,
    const StateAccess& access, int64_t created, ResponsesExecution& run);
struct AgentSessionView {
  io::Json session;
  io::Json turn;
  io::Json items;
};
ObjectReply ReadAgentSessionTurn(ObjectStore& objects, const std::string& id,
                                 std::string_view turn_id,
                                 const StateAccess& access,
                                 AgentSessionView* view);
class ResponsesJobs;
void ServeAgentSession(int fd, ObjectStore& objects, ResponsesJobs* jobs,
                       std::string_view method, std::string_view target,
                       const std::string& body, const ChatPlanConfig& config,
                       const StateAccess& access);
}  // namespace q4t::server
