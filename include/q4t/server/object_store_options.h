#pragma once

#include <string_view>

#include "q4t/server/object_store.h"
#include "q4t/server/server_options.h"

namespace q4t::server {

// Pure configuration: no stat/open/mkdir on the serving/model owner thread.
// The isolated worker creates/checks the private directory on first use.
ObjectStoreOptions ResolveObjectStoreOptions(const ServerOptions& options,
                                             std::string_view xdg_state_home,
                                             std::string_view home_directory);
Status ValidateObjectStoreOptions(const ServerOptions& options);

}  // namespace q4t::server
