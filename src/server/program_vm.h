#pragma once

#include <cstddef>
#include <functional>
#include <string_view>

#include "q4t/io/json.h"

namespace q4t::server::detail {
// Called exclusively inside the sandboxed worker after input bounds checks.
inline constexpr size_t kMaxActionProgressBytes = 4096;
inline constexpr size_t kMaxActionFrameBytes = 64 * 1024;
inline constexpr size_t kMaxActionStreamBytes = 512 * 1024;
inline constexpr unsigned kMaxActionProgressFrames = 64;
using ProgramFrameSink = std::function<bool(const io::Json&)>;
io::Json EvaluateProgram(const io::Json& input,
                         const ProgramFrameSink& progress = {});
}  // namespace q4t::server::detail
