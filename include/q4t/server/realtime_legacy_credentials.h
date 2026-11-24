#pragma once

#include "q4t/server/object_store.h"

namespace q4t::server {
ObjectReply LegacyRealtimeExpiry(const io::Json& body, std::string_view field,
                                 io::Json* expiry);
ObjectReply LegacyConversationCredential(const io::Json& body, io::Json* grant,
                                         double* temperature);
io::Json LegacyConversationResource(const io::Json& session,
                                    const io::Json* requested_modalities,
                                    double temperature);
}  // namespace q4t::server
