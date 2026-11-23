#pragma once

#include "q4t/server/agent_environment.h"

namespace q4t::server::environment_detail {
ObjectReply SealDefinition(ObjectStore& objects, const StateAccess& access,
                           const std::string& id, const io::Json& definition,
                           io::Json* sealed);
ObjectReply OpenDefinition(ObjectStore& objects, const StateAccess& access,
                           const std::string& id, const io::Json& sealed,
                           io::Json* definition);
}  // namespace q4t::server::environment_detail
