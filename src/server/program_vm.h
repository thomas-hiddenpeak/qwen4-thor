#pragma once

#include <cstddef>
#include <functional>
#include <string_view>

#include "q4t/io/json.h"

namespace q4t::server::detail {
// Called exclusively inside the sandboxed worker after input bounds checks.
inline constexpr size_t kMaxActionProgressBytes = 4096;
inline constexpr unsigned kMaxActionProgressFrames = 64;
using ProgramProgress = std::function<bool(std::string_view)>;
io::Json EvaluateProgram(const io::Json& input,
                         const ProgramProgress& progress = {});
}  // namespace q4t::server::detail
