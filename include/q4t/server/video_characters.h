#pragma once

#include "q4t/server/responses_state.h"

namespace q4t::server {
ObjectReply HandleVideoCharacter(ObjectStore& objects,
                                 const StateAccess& access,
                                 std::string_view method, std::string_view path,
                                 std::string_view content_type,
                                 std::string_view body);
}  // namespace q4t::server
