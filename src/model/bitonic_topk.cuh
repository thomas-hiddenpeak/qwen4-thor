// Private exact comparison network shared by streaming and decode top-k.
#pragma once

#include <cuda_runtime.h>

namespace q4t::model::detail {

// The same sorting network, with warp-local stages kept in registers. Each
// comparator keeps its original strict comparison: equal scores retain the
// same IDs as BitonicSortAsc, including ties at the selection boundary.
// Requires a 256-thread block and a whole number of warps per shared row.
template <int N>
__device__ void BitonicSortAscWarp(float* values, int* indices) {
  static_assert(N >= 256 && (N & (N - 1)) == 0);
  constexpr int kItems = N / 256;
  float v[kItems];
  int id[kItems];
#pragma unroll
  for (int r = 0; r < kItems; ++r) {
    const int i = threadIdx.x + r * 256;
    v[r] = values[i];
    id[r] = indices[i];
  }
#pragma unroll
  for (int k = 2; k <= N; k <<= 1) {
    // These partners belong to the same thread's register array.
#pragma unroll
    for (int j = k >> 1; j >= 256; j >>= 1) {
#pragma unroll
      for (int r = 0; r < kItems; ++r) {
        const int p = r ^ (j / 256);
        const int i = threadIdx.x + r * 256;
        if (r < p && ((i & k) == 0 ? v[r] > v[p] : v[r] < v[p])) {
          const float value = v[r];
          v[r] = v[p];
          v[p] = value;
          const int index = id[r];
          id[r] = id[p];
          id[p] = index;
        }
      }
    }
    if (k >= 64) {
      // Only cross-warp stages need shared memory and CTA barriers.
#pragma unroll
      for (int r = 0; r < kItems; ++r) {
        const int i = threadIdx.x + r * 256;
        values[i] = v[r];
        indices[i] = id[r];
      }
      __syncthreads();
#pragma unroll
      for (int j = min(k >> 1, 128); j >= 32; j >>= 1) {
        for (int pair = threadIdx.x; pair < N / 2; pair += 256) {
          const int i = 2 * (pair & ~(j - 1)) + (pair & (j - 1));
          const int p = i + j;
          const bool ascending = (i & k) == 0;
          if (ascending ? values[i] > values[p] : values[i] < values[p]) {
            const float value = values[i];
            values[i] = values[p];
            values[p] = value;
            const int index = indices[i];
            indices[i] = indices[p];
            indices[p] = index;
          }
        }
        __syncthreads();
      }
#pragma unroll
      for (int r = 0; r < kItems; ++r) {
        const int i = threadIdx.x + r * 256;
        v[r] = values[i];
        id[r] = indices[i];
      }
      // All shared reads must finish before a later stage overwrites it.
      __syncthreads();
    }
#pragma unroll
    for (int r = 0; r < kItems; ++r) {
      const int i = threadIdx.x + r * 256;
#pragma unroll
      for (int j = min(k >> 1, 16); j > 0; j >>= 1) {
        const float peer_value = __shfl_xor_sync(0xffffffff, v[r], j);
        const int peer_index = __shfl_xor_sync(0xffffffff, id[r], j);
        const bool keep_smaller = ((i & j) == 0) == ((i & k) == 0);
        if (keep_smaller ? v[r] > peer_value : v[r] < peer_value) {
          v[r] = peer_value;
          id[r] = peer_index;
        }
      }
    }
  }
#pragma unroll
  for (int r = 0; r < kItems; ++r) {
    const int i = threadIdx.x + r * 256;
    values[i] = v[r];
    indices[i] = id[r];
  }
  __syncthreads();
}

}  // namespace q4t::model::detail
