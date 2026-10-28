#pragma once

#include <algorithm>
#include <cstddef>
#include <span>

#include "q4t/status.h"

namespace q4t::server {

// The JSON contract checks type/range/scope first. Zero means read-only.
// A seed is optional: an existing longer session may skip this boundary.
inline Status ValidatePrefixSeedBoundary(int tokens,
                                         std::span<const int> allowed,
                                         size_t prompt_tokens) {
  if (tokens == 0) return Status();
  if (!std::binary_search(allowed.begin(), allowed.end(), tokens))
    return Status::Fail("prefix_cache_write_tokens is not an allowed checkpoint");
  if (static_cast<size_t>(tokens) >= prompt_tokens)
    return Status::Fail("prefix cache seed requires an uncached prompt token");
  return Status();
}

}  // namespace q4t::server
