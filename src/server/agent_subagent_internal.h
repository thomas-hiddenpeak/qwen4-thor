#pragma once

#include <map>

#include "q4t/server/agent_session.h"
#include "q4t/server/application_routes.h"
#include "q4t/server/conversation_state.h"

namespace q4t::server::agent_detail {
struct ChildOutputLocation {
  std::string turn;
  size_t index = 0;
};
struct SubagentStepEvents {
  std::map<size_t, ChildOutputLocation> outputs;
  std::map<size_t, std::vector<io::Json>> item_done;
  std::map<size_t, std::vector<io::Json>> claims;
  std::vector<io::Json> terminal;
};
ObjectReply IndexSubagentEvents(const io::Json& session, const io::Json& record,
                                SubagentStepEvents* events);
// Canonical native function pairs produced by the managed lifecycle runner.
ObjectReply ReadSubagentOpening(ObjectStore& objects,
                                const std::string& session,
                                const std::string& path,
                                const StateAccess& access, int64_t* opened);
bool IsManagedAgentOperation(const io::Json& item);
bool IsChildAgentOutput(const io::Json& item);
bool IsAgentOperationResult(const io::Json& item);
bool IsPrivateAgentOutput(const io::Json& item);
ObjectReply ParentActionItem(const io::Json& native, const io::Json& response,
                             const io::Json& session, const std::string& turn,
                             io::Json* items);
ObjectReply ReadSessionSubagents(ObjectStore& objects,
                                 const AgentSessionRoute& route,
                                 const ItemViewOptions& options,
                                 const StateAccess& access);
}  // namespace q4t::server::agent_detail
