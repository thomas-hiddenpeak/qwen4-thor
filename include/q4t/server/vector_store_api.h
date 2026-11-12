// Public native retrieval management. Execution uses the durable job scanner.
#pragma once

#include "q4t/server/client_policy.h"
#include "q4t/server/vector_stores.h"

namespace q4t::server {
ObjectReply HandleVectorStoreRequest(
    ObjectStore& objects, ClientAdmission& clients, bool ingestion_configured,
    std::string_view method, std::string_view target, const std::string& body,
    const ClientIdentity& client, const BinaryAccess& access);
}  // namespace q4t::server
