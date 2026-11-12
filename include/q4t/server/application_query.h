#pragma once

#include <string_view>

#include "q4t/server/object_store.h"

namespace q4t::server {
// Strict bounded URL query decoding shared by native application endpoints.
// Semantic field allowlists are applied by each endpoint after decoding.
ObjectReply ParseApplicationQuery(std::string_view query);
}  // namespace q4t::server
