#pragma once

#include "q4t/server/responses_state.h"

namespace q4t::server {
// Publication is available only through the local operator socket. Public
// retrieval derives the project from the authenticated request.
ObjectReply PublishSafetyRecord(ObjectStore& store, const io::Json& command,
                                const StateAccess& access);
ObjectReply GetSafetyRecord(ObjectStore& store, const StateAccess& access,
                            std::string_view method, std::string_view target,
                            std::string_view body);
int MaybeRunSafetyCommand(int argc, char** argv);
}  // namespace q4t::server
