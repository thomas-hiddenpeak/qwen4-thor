#pragma once

#include <string>
#include <string_view>
#include <utility>
#include <vector>

namespace q4t::server {
// Worker-only helpers. Exceptions retain the existing invalid/resource split.
std::string CustomRegexEbnf(std::string_view pattern);
struct CustomLarkSource {
  std::string text;
  std::vector<std::pair<std::string, std::string>> regexes;
};
CustomLarkSource PrepareCustomLark(std::string_view source);
}  // namespace q4t::server
