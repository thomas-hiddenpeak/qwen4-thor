#pragma once

#include "q4t/server/chatkit.h"

namespace q4t::server::chatkit_detail {
// A model job and its pending thread revision share a journal transaction.
// Any reader can finish a completed response after a model or server restart.
ObjectReply ReadThread(ObjectStore& objects, const StateAccess& access,
                       const std::string& id, std::string_view user = {});
io::Json ClientThread(const io::Json& data);
io::Json ClientItem(io::Json item);
}  // namespace q4t::server::chatkit_detail
