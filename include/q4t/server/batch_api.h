// Native public Batch transport shares the durable task owner with recovery.
#pragma once

#include "q4t/server/batch_jobs.h"

namespace q4t::server {
ObjectReply HandleBatchRequest(ObjectStore& objects, BatchJobs* jobs,
                               std::string_view method, std::string_view target,
                               const std::string& body,
                               const ClientIdentity& client,
                               const BinaryAccess& access);
}  // namespace q4t::server
