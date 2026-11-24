#include "agent_environment_internal.h"

#include "agent_definition_cipher.h"
#include "object_protocol.h"

namespace q4t::server::environment_detail {
using agent_detail::DefinitionKind;
ObjectReply SealDefinition(ObjectStore& objects, const StateAccess& access,
                           const std::string& id, const io::Json& value,
                           io::Json* sealed) {
  return agent_detail::SealDefinition(objects, access,
                                      DefinitionKind::kEnvironmentTemplate, id,
                                      value, sealed);
}

ObjectReply OpenDefinition(ObjectStore& objects, const StateAccess& access,
                           const std::string& id, const io::Json& sealed,
                           io::Json* value) {
  auto reply = agent_detail::OpenDefinition(
      objects, access, DefinitionKind::kEnvironmentTemplate, id, sealed, value);
  if (!reply.ok()) return reply;
  ChatPlanError error;
  if (!ValidateEnvironmentTemplate(*value,
                                   EnvironmentTemplateShape::kStoredDefinition,
                                   &error))
    return object_detail::Error(503, "environment_template_invalid",
                                "Invalid stored environment definition");
  return {};
}
}  // namespace q4t::server::environment_detail
