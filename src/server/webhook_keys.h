#pragma once

#include "q4t/server/responses_state.h"

namespace q4t::server::webhook_detail {
// The durable keyring is sealed under the owner and endpoint ID. An entry
// with no expiry is current; older entries may sign during a rotation grace.
ObjectReply ReadKeys(ObjectStore& objects, const StateAccess& access,
                     const std::string& id, const io::Json& record, int64_t now,
                     io::Json* active);
ObjectReply Rotate(ObjectStore& objects, const StateAccess& access,
                   const std::string& id, int64_t now, bool keep_old,
                   io::Json keys, io::Json* record, std::string* secret);
}  // namespace q4t::server::webhook_detail
