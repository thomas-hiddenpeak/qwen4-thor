// Shared JSON budgets for request bodies and JSON-encoded tool arguments.
#pragma once

#include "q4t/io/json.h"

namespace q4t::server {

inline constexpr io::JsonParseLimits kRequestJsonLimits{
    .max_depth = 64,
    .max_values = 65536,
    .max_string_bytes = 16 * 1024 * 1024,
    .reject_duplicate_keys = true};

}  // namespace q4t::server
