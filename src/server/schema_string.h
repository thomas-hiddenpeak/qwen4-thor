#pragma once

#include <chrono>
#include <string_view>

#include "q4t/io/json.h"
#include "q4t/status.h"

namespace q4t::server {

// Bounded independent final-value checker. All nine official string formats
// are local syntax/value checks; no DNS, network, locale or external service.
bool SchemaFormatValid(std::string_view format, std::string_view value);
// Compare an RFC 3339 timestamp with a system instant, preserving its offset
// and subsecond precision. Invalid timestamps fail closed.
bool SchemaDateTimeAfter(std::string_view value,
                         std::chrono::system_clock::time_point now);
Status CheckSchemaString(const io::Json& schema, std::string_view value,
                         size_t* work_remaining, bool* accepted);

}  // namespace q4t::server
