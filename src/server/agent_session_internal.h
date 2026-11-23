#pragma once

#include "q4t/server/agent_session.h"

namespace q4t::server::agent_detail {
// Return the immutable public settings bound to a step, or the latest settings
// when no link is supplied. Never resolve them via a mutable Agent
// registration.
ObjectReply SessionSettings(const io::Json& data,
                            const io::Json* link = nullptr);
ObjectReply BindSessionSettings(io::Json* data, const io::Json& session,
                                io::Json* link = nullptr);
ObjectReply ProjectItem(const io::Json& from, const std::string& turn_id,
                        io::Json* items, io::Json* actions, bool completed);
ObjectReply ProjectStep(const io::Json& session, const io::Json& link,
                        const io::Json& record, AgentSessionView* view);
bool AddUsage(io::Json* total, const io::Json& usage);
ObjectReply ReadRow(ObjectStore& objects, const std::string& id,
                    const StateAccess& access);
}  // namespace q4t::server::agent_detail
