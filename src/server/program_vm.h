#pragma once

#include "q4t/io/json.h"

namespace q4t::server::detail {
// Called exclusively inside the sandboxed worker after input bounds checks.
io::Json EvaluateProgram(const io::Json& input);
}  // namespace q4t::server::detail
