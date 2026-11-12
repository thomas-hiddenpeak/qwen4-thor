#pragma once

#include <string_view>

#include "q4t/io/json.h"
#include "q4t/status.h"

namespace q4t::server {

// Bounded independent final-value checker. All nine official string formats
// are local syntax/value checks; no DNS, network, locale or external service.
bool SchemaFormatValid(std::string_view format, std::string_view value);
Status CheckSchemaString(const io::Json& schema, std::string_view value,
                         size_t* work_remaining, bool* accepted);

}  // namespace q4t::server
