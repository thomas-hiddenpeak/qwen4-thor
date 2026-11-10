// Application transport ceilings. Client/ingress policy may set lower limits.
#pragma once

#include <cstddef>

namespace q4t::server {
// A permitted 50,000,000-byte input file needs 66,666,668 base64 bytes, plus
// the data-URI and JSON envelope. Keep one bounded ceiling across these layers.
inline constexpr size_t kChatBodyBytes = 96ULL << 20;
inline constexpr size_t kAggregateChatBodyBytes = 192ULL << 20;
// A prompt expansion retains both the original variables and expanded input.
inline constexpr size_t kExpandedApplicationBytes = 192ULL << 20;
inline constexpr size_t kCancelBodyBudgetBytes = 32ULL << 20;
inline constexpr size_t kBackgroundInputBytes = 512ULL << 20;
inline constexpr size_t kMaxBackgroundInputBytes = 2ULL << 30;
}  // namespace q4t::server
