// Minimal logging for q4t.
//
// INFO goes to stderr (stdout is reserved for machine-readable output such as
// probe/version JSON). Keep messages short and factual.
#pragma once

#include <cstdio>

namespace q4t {
inline void LogInfo(const char* fmt, ...) {
  std::va_list args;
  va_start(args, fmt);
  std::fprintf(stderr, "[info] ");
  std::vfprintf(stderr, fmt, args);
  std::fprintf(stderr, "\n");
  va_end(args);
}
inline void LogWarn(const char* fmt, ...) {
  std::va_list args;
  va_start(args, fmt);
  std::fprintf(stderr, "[warn] ");
  std::vfprintf(stderr, fmt, args);
  std::fprintf(stderr, "\n");
  va_end(args);
}
inline void LogError(const char* fmt, ...) {
  std::va_list args;
  va_start(args, fmt);
  std::fprintf(stderr, "[error] ");
  std::vfprintf(stderr, fmt, args);
  std::fprintf(stderr, "\n");
  va_end(args);
}
}  // namespace q4t
