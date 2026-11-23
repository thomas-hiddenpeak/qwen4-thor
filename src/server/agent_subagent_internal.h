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
bool IsPrivateAgentOutput(const io::Json& item);
ObjectReply ParentActionItem(const io::Json& native, const io::Json& response,
                             const io::Json& session, const std::string& turn,
                             io::Json* items);
ObjectReply ReadSessionSubagents(ObjectStore& objects,
                                 const AgentSessionRoute& route,
                                 const ItemViewOptions& options,
                                 const StateAccess& access);
}  // namespace q4t::server::agent_detail
