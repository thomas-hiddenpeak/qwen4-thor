// Live Responses configuration belongs to the backend, including credentials.
#pragma once

#include "q4t/server/live_shape.h"
#include "q4t/server/object_store.h"

namespace q4t::server {
// Extend the pinned SDK's narrow tool declaration union for the tools in the
// Live delegation guide. The actual tools are checked by the Responses owner.
Status ValidateLiveWireShape(const io::Json& value, LiveShape shape,
                             ChatPlanError* error,
                             std::string_view path = "event");
// Called only on public copies. The session's private settings remain intact.
void RedactLiveResponseCredentials(io::Json* session);
// Build an execution request using only the backend settings and supplied
// history. Accepts no caller-controlled store, stream or Responses identity.
Status MakeLiveResponsesRequest(const io::Json& settings, io::Json input,
                                io::Json* request, ChatPlanError* error);
Status CheckLiveResponsesSettings(
    const io::Json& settings, const ChatPlanConfig& config,
    std::string_view scope, ChatPlanError* error,
    const std::function<ObjectReply(const std::string&)>& check_store = {});
}  // namespace q4t::server
