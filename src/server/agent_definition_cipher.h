#pragma once

#include "q4t/server/responses_state.h"

namespace q4t::server::agent_detail {
enum class DefinitionKind { kEnvironmentTemplate, kAutomationTarget };
ObjectReply SealDefinition(ObjectStore& objects, const StateAccess& access,
                           DefinitionKind kind, const std::string& id,
                           const io::Json& value, io::Json* sealed);
ObjectReply OpenDefinition(ObjectStore& objects, const StateAccess& access,
                           DefinitionKind kind, const std::string& id,
                           const io::Json& sealed, io::Json* value);
}  // namespace q4t::server::agent_detail
