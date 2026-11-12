// Protocol transport ceilings; deployment and per-client quotas remain lower.
#pragma once

#include <cstddef>

namespace q4t::server {
inline constexpr size_t kFileBytes = 512ULL * 1024 * 1024;
inline constexpr size_t kFileWireBytes = kFileBytes + 65536;
}  // namespace q4t::server
