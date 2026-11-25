#pragma once

#include "q4t/server/agent_session.h"

namespace q4t::server::agent_detail {
void ApplyPromptCacheOptions(io::Json* session, const io::Json& request);
ObjectReply SealAgentHttp(ObjectStore& objects, const StateAccess& access,
                          std::string_view session_id, const io::Json& settings,
                          std::string* sealed);
// Return the immutable public settings bound to a step, or the latest settings
// when no link is supplied. Never resolve them via a mutable Agent
// registration.
ObjectReply SessionSettings(const io::Json& data,
                            const io::Json* link = nullptr);
ObjectReply BindSessionSettings(io::Json* data, const io::Json& session,
                                io::Json* link = nullptr);
io::Json SavedAgentToolResult(const std::string& response_id,
                              const io::Json& event);
bool IsAgentDiscoveryItem(std::string_view type);
ObjectReply ReconcileSessionDiscovery(const io::Json& tools, io::Json* history);
ObjectReply ProjectItem(const io::Json& from, const std::string& turn_id,
                        io::Json* items, io::Json* actions, bool completed);
ObjectReply ProjectStep(const io::Json& session, const io::Json& link,
                        const io::Json& record, AgentSessionView* view);
ObjectReply ProjectHistory(ObjectStore& objects, const io::Json& data,
                           const std::string& id, std::string_view turn_id,
                           const StateAccess& access, AgentSessionView* view,
                           bool all_items);
bool AddUsage(io::Json* total, const io::Json& usage);
ObjectReply ReadRow(ObjectStore& objects, const std::string& id,
                    const StateAccess& access);
}  // namespace q4t::server::agent_detail
