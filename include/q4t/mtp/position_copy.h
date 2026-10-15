#pragma once

#include <cuda_runtime_api.h>

#include <cstddef>

namespace q4t::mtp::detail {

// rows is positive; destination is a device buffer. UVA permits either a
// host or device source. Both buffers remain owned until stream completion.
inline cudaError_t CopyPositionsForForward(int* destination, const int* source,
                                           int rows, cudaStream_t stream) {
  return cudaMemcpyAsync(destination, source,
                          static_cast<size_t>(rows) * sizeof(int),
                          cudaMemcpyDefault, stream);
}

}  // namespace q4t::mtp::detail
