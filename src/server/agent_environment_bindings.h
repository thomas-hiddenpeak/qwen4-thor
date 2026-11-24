#pragma once

#include <algorithm>
#include <cstddef>
#include <string_view>

#include "q4t/io/json.h"

namespace q4t::server::environment_detail {
inline constexpr std::size_t kMaxAttachedSessions = 128;

inline bool HasAttachedSession(const io::Json& data, std::string_view id) {
  if (id.empty()) return false;
  const auto* sessions = data.GetArray("attached_sessions");
  if (!sessions) return data.GetString("bound_session") == id;
  return std::ranges::any_of(sessions->array, [&](const io::Json& value) {
    return value.IsString() && value.str == id;
  });
}
}  // namespace q4t::server::environment_detail
