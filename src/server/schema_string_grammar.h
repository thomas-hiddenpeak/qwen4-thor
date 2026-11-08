#pragma once

#include <string>
#include <vector>

#include "q4t/io/json.h"

namespace q4t::server {

// Worker-only syntax language. Length/pattern and regular format constraints
// are intersected before JSON encoding. The worker's semantic token guard is
// mandatory for IDNA contexts and timezone/leap-second relationships.
std::string CompileSchemaStringSyntax(
    const std::vector<const io::Json*>& terms);

}  // namespace q4t::server
