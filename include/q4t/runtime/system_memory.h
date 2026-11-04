// Linux-wide estimates, not a reservation or a CUDA/cgroup allocation limit.
#pragma once

#include <cstdint>
#include <string_view>

namespace q4t::runtime {

struct SystemMemory {
  bool valid = false;
  uint64_t total_bytes = 0;
  uint64_t available_bytes = 0;

  // Unknown and insufficient observations both decline speculative allocation.
  bool CanAllocate(uint64_t bytes) const {
    return valid && available_bytes >= bytes;
  }
};

// Strict, bounded /proc/meminfo parsing. Invalid observations contain no values.
SystemMemory ParseSystemMemory(std::string_view text);
SystemMemory ReadSystemMemory();

}  // namespace q4t::runtime
