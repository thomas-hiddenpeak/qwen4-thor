#pragma once

#include <string>

#include "q4t/io/json.h"

namespace q4t::server {
// Worker-only exact canonical decimal language. Equivalent exponent spellings
// are accepted by the independent value checker; generation uses plain decimal.
std::string SchemaNumberGrammar(const io::Json& schema, bool integer);
}  // namespace q4t::server
