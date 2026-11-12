#pragma once

#include "q4t/io/json.h"

namespace q4t::server {
enum class ResponseOwnerState { kAlive, kGone, kUnknown };
// Local process identity, independent of the disposable object worker. A
// missing monitor or unavailable /proc is unknown, never proof of death.
io::Json CurrentResponseOwner();
bool ValidResponseOwner(const io::Json& owner);
ResponseOwnerState ResponseOwnerStatus(const io::Json& owner);
}  // namespace q4t::server
