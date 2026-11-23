#pragma once

#include <string_view>

namespace q4t::server {
inline bool ValidReferTarget(std::string_view uri) {
  // RFC 3515 Refer-To carries a URI. Keep percent escapes intact when it is
  // placed inside a name-addr; angle brackets and whitespace delimit headers.
  constexpr std::string_view letters =
      "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz";
  constexpr std::string_view digits = "0123456789";
  constexpr std::string_view hex = "0123456789ABCDEFabcdef";
  constexpr std::string_view scheme_chars =
      "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+.-";
  const auto colon = uri.find(':');
  if (uri.size() > 8192 || colon == uri.npos || !colon ||
      colon + 1 == uri.size() || letters.find(uri.front()) == letters.npos ||
      uri.substr(1, colon - 1).find_first_not_of(scheme_chars) != uri.npos)
    return false;
  constexpr std::string_view punctuation = "-._~:/?#[]@!$&'()*+,;=";
  for (size_t i = colon + 1; i < uri.size(); ++i) {
    const char c = uri[i];
    if (c == '%') {
      if (i + 2 >= uri.size() || hex.find(uri[i + 1]) == hex.npos ||
          hex.find(uri[i + 2]) == hex.npos)
        return false;
      i += 2;
    } else if (letters.find(c) == letters.npos &&
               digits.find(c) == digits.npos &&
               punctuation.find(c) == punctuation.npos) {
      return false;
    }
  }
  return true;
}
}  // namespace q4t::server
