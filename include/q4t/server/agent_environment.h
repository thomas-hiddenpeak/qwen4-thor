#pragma once

#include "q4t/server/chat_plan.h"
#include "q4t/server/responses_state.h"

namespace q4t::server {
enum class EnvironmentTemplateShape { kCreate, kUpdate, kResource };
Status ValidateEnvironmentTemplate(const io::Json& value,
                                   EnvironmentTemplateShape shape,
                                   ChatPlanError* error);
ObjectReply HandleEnvironmentTemplate(ObjectStore& objects,
                                      std::string_view method,
                                      std::string_view target,
                                      const std::string& body,
                                      const StateAccess& access);
}  // namespace q4t::server
