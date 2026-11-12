#pragma once

#include "q4t/server/responses_state.h"

namespace q4t::server {

bool IsResultBoundToolCall(const io::Json& item);
// Private invocation-time bindings. Null schema means explicitly unbound, not
// permission to inherit a later request's declaration. No public item changes.
ObjectReply MergeToolResultContracts(const io::Json& source,
                                     const io::Json& items,
                                     io::Json* contracts);
ObjectReply BindToolResultContracts(const io::Json& request,
                                    const io::Json& items, io::Json* contracts,
                                    bool authenticated_program = false);
ObjectReply CheckToolResultContracts(const io::Json& request,
                                     const io::Json& history,
                                     const io::Json& contracts,
                                     const StateAccess& access);

}  // namespace q4t::server
