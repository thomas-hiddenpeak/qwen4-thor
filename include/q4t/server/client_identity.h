// Internal client identities are supplied only by an authenticated gateway.
#pragma once

#include <string>
#include <string_view>

namespace q4t::server {

inline bool ValidClientId(std::string_view id) {
  if (id.empty() || id.size() > 32 || id.front() < 'a' || id.front() > 'z')
    return false;
  for (char c : id)
    if (!((c >= 'a' && c <= 'z') || (c >= '0' && c <= '9') || c == '_' ||
          c == '-'))
      return false;
  return true;
}

// The external request/session/scope validators forbid NUL. The leading NUL
// makes the tuple disjoint from legacy keys, and the second separates client
// from external key. Never use this internal key as an HTTP ID or log string.
// Empty keys remain empty so scoping does not accidentally enable a cache.
inline std::string ClientScopedKey(std::string_view client,
                                   std::string_view external) {
  if (external.empty() || client.empty()) return std::string(external);
  std::string result(1, '\0');
  result.append(client);
  result.push_back('\0');
  result.append(external);
  return result;
}

}  // namespace q4t::server
