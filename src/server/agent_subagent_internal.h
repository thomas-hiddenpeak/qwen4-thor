#pragma once

#include "q4t/server/agent_session.h"
#include "q4t/server/application_routes.h"
#include "q4t/server/conversation_state.h"

namespace q4t::server::agent_detail {
bool IsPrivateAgentOutput(const io::Json& item);
ObjectReply ParentActionItem(const io::Json& native, const io::Json& response,
                             const io::Json& session, const std::string& turn,
                             io::Json* items);
ObjectReply ReadSessionSubagents(ObjectStore& objects,
                                 const AgentSessionRoute& route,
                                 const ItemViewOptions& options,
                                 const StateAccess& access);
}  // namespace q4t::server::agent_detail
