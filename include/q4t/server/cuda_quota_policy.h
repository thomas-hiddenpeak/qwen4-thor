#pragma once

#include <cmath>
#include <cstdint>
#include <limits>

#include "q4t/runtime/system_memory.h"
#include "q4t/status.h"

namespace q4t::server {

// Pure host resolution. Auto (-1) uses total RAM for a requested-byte ceiling;
// it is not an available-memory estimate, physical reservation, or OOM proof.
inline Status ResolveCudaQuotaLimit(int requested_mib, double fraction,
                                    runtime::SystemMemory memory,
                                    uint64_t* output) {
  if (!output || requested_mib < -1 || requested_mib > 1048576)
    return Status::Fail("invalid CUDA allocation quota");
  if (requested_mib >= 0) {
    *output = uint64_t(requested_mib) * 1024 * 1024;
    return {};
  }
  if (!memory.valid || !memory.total_bytes ||
      memory.available_bytes > memory.total_bytes || !std::isfinite(fraction) ||
      fraction <= 0 || fraction > 1)
    return Status::Fail("CUDA allocation quota auto resolution unavailable");
  const long double value =
      static_cast<long double>(memory.total_bytes) * fraction;
  if (value < 1 || value > std::numeric_limits<uint64_t>::max())
    return Status::Fail("CUDA allocation quota auto resolution overflow");
  *output = static_cast<uint64_t>(value);
  return {};
}
}  // namespace q4t::server
