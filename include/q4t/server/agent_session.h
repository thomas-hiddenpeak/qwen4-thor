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
                                const StateAccess& access,
                                io::Json* private_http = nullptr,
                                bool* tool_search = nullptr);
ObjectReply NormalizeSessionAgent(io::Json settings, const io::Json& overrides,
                                  const ChatPlanConfig& config,
                                  io::Json* private_http = nullptr,
                                  bool* tool_search = nullptr);
struct AgentSessionStart {
  io::Json session;
  io::Json input;
  std::string sealed_http{};
  std::optional<ObjectRequest> environment_write{};
  std::shared_ptr<environment_detail::WorkspaceCopies> workspace_copies{};
  bool tool_search = false;
};

// Prepare the public snapshot and any workspace claim before publication.
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
  io::Json turns;
};
ObjectReply ReadAgentSessionTurn(ObjectStore& objects, const std::string& id,
                                 std::string_view turn_id,
                                 const StateAccess& access,
                                 AgentSessionView* view,
                                 bool all_items = false);
struct ItemViewOptions;
ObjectReply ListAgentSessionTraces(ObjectStore& objects, const std::string& id,
                                   const ItemViewOptions& paging,
                                   const StateAccess& access);
// Prepare the next model step from authorized session events. A repeated
// idempotency key with an identical committed payload returns replay=true.
ObjectReply PrepareAgentSessionEvents(
    ObjectStore& objects, const std::string& id, const io::Json& events,
    std::string_view key, const ChatPlanConfig& config,
    const StateAccess& access, ResponsesExecution& run, bool* replay);
ObjectReply ReconcileAgentEventReceipt(
    ObjectStore& objects, const std::string& id, const io::Json& request,
    std::string_view key, const StateAccess& access, ObjectReply result);
ObjectReply ListAgentSessions(ObjectStore& objects, const io::Json& query,
                              const StateAccess& access);
ObjectReply UpdateAgentSession(ObjectStore& objects, const std::string& id,
                               const io::Json& request,
                               const ChatPlanConfig& config,
                               const StateAccess& access);
class ResponsesJobs;
ObjectReply CancelAgentSessionTurn(ObjectStore& objects, ResponsesJobs* jobs,
                                   const std::string& id,
                                   const io::Json& events, std::string_view key,
                                   const StateAccess& access);
struct AgentEventCursor {
  // A native step finished with outstanding client actions. A later session
  // cancellation can still produce a logical terminal for this step.
  bool waiting = false;
  bool created_seen = false;
  size_t step = 0;
  size_t sequence = 0;
  std::string turn_id;
  io::Json session_usage;
  io::Json turn_usage;
  int64_t created_at = 0;
  int64_t started_at = 0;
};
// Install the live subscription boundary before acknowledging GET /events.
ObjectReply OpenAgentEventSubscription(ObjectStore& objects,
                                       const std::string& id,
                                       const StateAccess& access,
                                       AgentEventCursor* cursor);
// Project the next retained native events. A create subscriber completes at
// its logical turn terminal. A tool request leaves it attached to the session.
ObjectReply ReplayAgentSessionEvents(ObjectStore& objects,
                                     const std::string& id,
                                     const StateAccess& access,
                                     AgentEventCursor* cursor, io::Json* events,
                                     bool* settled);
class ClientAdmission;
void ServeAgentSession(int fd, ObjectStore& objects, ClientAdmission& clients,
                       ResponsesJobs* jobs, std::string_view method,
                       std::string_view target, const std::string& body,
                       const ChatPlanConfig& config, const StateAccess& access);
}  // namespace q4t::server
