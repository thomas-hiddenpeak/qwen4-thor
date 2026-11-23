#pragma once

#include "q4t/server/responses_state.h"

namespace q4t::server {
ObjectReply CheckAgentVaults(ObjectStore& objects, const StateAccess& access,
                             const io::Json& vaults);
ObjectReply HandleAgentVault(ObjectStore& objects, std::string_view method,
                             std::string_view target, const std::string& body,
                             const StateAccess& access);
}  // namespace q4t::server
