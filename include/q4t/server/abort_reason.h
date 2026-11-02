#pragma once

#include <array>
#include <cstddef>
#include <string_view>

namespace q4t::server {

// Fixed vocabulary only; request IDs, user input and error text are not labels.
inline constexpr std::array<std::string_view, 6> kAbortReasons{
    "client_disconnected", "explicit_cancel", "deadline_exceeded",
    "server_shutdown",     "request_failed",  "other"};

inline size_t AbortReasonIndex(std::string_view reason) {
  for (size_t i = 0; i + 1 < kAbortReasons.size(); ++i)
    if (reason == kAbortReasons[i]) return i;
  return kAbortReasons.size() - 1;
}

}  // namespace q4t::server
