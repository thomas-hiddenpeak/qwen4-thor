#pragma once
#include "q4t/server/responses_state.h"

namespace q4t::server {
enum class AgentRegistrationShape { kCreate, kUpdate, kResource, kDeleted };
Status ValidateAgentRegistration(const io::Json& value,
                                 AgentRegistrationShape shape,
                                 ChatPlanError* error);
ObjectReply HandleAgentRegistration(ObjectStore& objects,
                                    std::string_view method,
                                    std::string_view target,
                                    const std::string& body,
                                    const ChatPlanConfig& config,
                                    const StateAccess& access);
}  // namespace q4t::server
