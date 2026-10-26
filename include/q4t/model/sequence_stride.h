#pragma once

#include <cuda_runtime.h>
#include <cstddef>

namespace q4t::model {
struct IndexerSequenceStride {
  size_t uniform;
  const size_t* offsets;
  __host__ __device__
  IndexerSequenceStride(size_t stride, const size_t* explicit_offsets = nullptr)
      : uniform(stride), offsets(explicit_offsets) {}
  __host__ __device__ size_t Offset(int seq) const {
    return offsets ? offsets[seq] : static_cast<size_t>(seq) * uniform;
  }
};
}  // namespace q4t::model
