// Shared JSON budgets for request bodies and JSON-encoded tool arguments.
#pragma once

#include "q4t/io/json.h"
#include "q4t/server/request_limits.h"

namespace q4t::server {

inline constexpr io::JsonParseLimits kRequestJsonLimits{
    .max_depth = 64,
    .max_values = 65536,
    .max_string_bytes = 16 * 1024 * 1024,
    .reject_duplicate_keys = true};

// Chat/preflight and argument/schema data retain exact decimals. Numeric
// generation controls separately require finite, in-range values. Ordinary
// model/config/cancel parsing remains on the default limits above.
inline constexpr io::JsonParseLimits kSchemaJsonLimits{
    .max_depth = 64,
    .max_values = 65536,
    .max_string_bytes = 16 * 1024 * 1024,
    .reject_duplicate_keys = true,
    .allow_exact_number_text = true};

// File-bearing application envelopes have a separate budget. This does not
// enlarge embedded tool arguments, schema compilation, or control messages.
inline constexpr io::JsonParseLimits kApplicationJsonLimits{
    .max_depth = 64,
    .max_values = 65536,
    .max_string_bytes = kChatBodyBytes,
    .reject_duplicate_keys = true,
    .allow_exact_number_text = true};

}  // namespace q4t::server
