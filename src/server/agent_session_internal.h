#pragma once

#include "q4t/server/agent_session.h"

namespace q4t::server::agent_detail {
ObjectReply ProjectItem(const io::Json& from, const std::string& turn_id,
                        io::Json* items, io::Json* actions, bool completed);
ObjectReply ProjectStep(const io::Json& session, const io::Json& link,
                        const io::Json& record, AgentSessionView* view);
bool AddUsage(io::Json* total, const io::Json& usage);
ObjectReply ReadRow(ObjectStore& objects, const std::string& id,
                    const StateAccess& access);
}  // namespace q4t::server::agent_detail
