#pragma once

#include "q4t/server/state_cipher.h"

namespace q4t::server {
// Restores only authenticated, client-owned state. The immutable public input
// is not rewritten. Omitted origin prefixes are restored from the encrypted
// payload; supplied prefixes must agree with the authenticated origin.
ObjectReply OpenResponsesReasoning(StateCipher& cipher, const io::Json& history,
                                   const ChatPlanConfig& config,
                                   const StateAccess& access,
                                   io::Json* generation_history);
ObjectReply SealResponsesReasoning(StateCipher& cipher,
                                   const io::Json& generation_history,
                                   const io::Json& output, size_t index,
                                   const ChatPlanConfig& config,
                                   const StateAccess& access,
                                   std::string* opaque);
}  // namespace q4t::server
