#pragma once

#include <string>
#include <cstdint>
#include <string_view>
#include <utility>
#include <vector>

namespace q4t::server {
// Worker-only helpers. Exceptions retain the existing invalid/resource split.
std::string CustomRegexEbnf(std::string_view pattern);
// Schema patterns are full languages here. The caller applies unanchored
// search semantics explicitly; all intersections and exact multiples remain
// inside the existing grammar worker's allocation/deadline limits.
std::string SchemaRegexEbnf(const std::vector<std::string>& patterns,
                            uint32_t multiple = 0, int32_t scale = 0);
bool SchemaRegexMatches(std::string_view pattern, std::string_view value,
                        size_t* work_remaining);
struct CustomLarkSource {
  std::string text;
  std::vector<std::pair<std::string, std::string>> regexes;
};
CustomLarkSource PrepareCustomLark(std::string_view source);
}  // namespace q4t::server
