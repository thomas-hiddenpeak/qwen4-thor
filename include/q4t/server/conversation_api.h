#pragma once

#include <string_view>

#include "q4t/server/conversation_state.h"

namespace q4t::server {

bool IsConversationTarget(std::string_view target);

// The HTTP transport supplies authenticated authority and its bounded lifetime.
// This handler shares the native ObjectStore; it never forwards HTTP or starts
// an external application server. It is also usable by CPU transport fixtures.
ObjectReply HandleConversationRequest(ObjectStore& objects,
                                      std::string_view method,
                                      std::string_view target,
                                      const std::string& body,
                                      const StateAccess& access);

}  // namespace q4t::server
