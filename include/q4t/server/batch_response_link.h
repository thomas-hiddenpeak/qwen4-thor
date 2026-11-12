// Trusted durable parent identity. Never parsed from a public request body.
#pragma once

#include "q4t/server/object_store.h"

namespace q4t::server {
struct StateAccess;
struct BatchResponseLink {
  std::string batch_id, response_id, request_id, scope;
  size_t index = 0;
  bool operator==(const BatchResponseLink&) const = default;
};
io::Json BatchResponseLinkValue(const BatchResponseLink& link);
bool ParseBatchResponseLink(const io::Json& value, BatchResponseLink* link);
// Checks the original running row and reports its durable cancellation or
// expiry. A completed parent never grants a second execution.
ObjectReply CheckBatchResponseParent(ObjectStore& objects,
                                     const BatchResponseLink& link,
                                     const StateAccess& access,
                                     bool* cancelled);
}  // namespace q4t::server
