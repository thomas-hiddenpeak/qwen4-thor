#pragma once

#include "q4t/server/responses_state.h"

namespace q4t::server {
Status ValidateEval(const io::Json& value, const char* root,
                    ChatPlanError* error);
ObjectReply HandleEval(ObjectStore& objects, const StateAccess& access,
                       std::string_view method, std::string_view target,
                       const std::string& body);
}  // namespace q4t::server
