// Serving uses the same allocation/ownership policy as model-side media.
#pragma once

#include "q4t/runtime/device_memory.h"

namespace q4t::server {
using runtime::AllocateDeviceMemory;
using runtime::ApplyDeviceMemoryFailure;
using runtime::DeviceMemoryResult;
using runtime::DeviceMemoryState;
using runtime::ReleaseDeviceMemory;
}  // namespace q4t::server
