#pragma once

#include "agent_automation_internal.h"

namespace q4t::server::automation_detail {
inline constexpr char kToolName[] = "manage_automation";

// Responses-format function declaration for sessions that enable automations.
io::Json SessionTool();
bool IsToolItem(const io::Json& item);
ObjectReply PlanSessionTool(ObjectStore& objects, const StateAccess& access,
                            const ChatPlanConfig& config, const io::Json& args,
                            int64_t now, bool dispatcher_available,
                            SessionAutomationChange* caller);
}  // namespace q4t::server::automation_detail
