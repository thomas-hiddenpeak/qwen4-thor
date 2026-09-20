// Exact streaming top-k merge, isolated from the other attention kernels.
#include "q4t/model/streaming_topk.h"

namespace q4t::model {
namespace {

constexpr int kMaxBlocks = 2048;
constexpr int kMaxBlockTopk = 512;

// The same sorting network, with warp-local stages kept in registers. Each
// comparator keeps its original strict comparison: equal scores retain the
// same IDs as BitonicSortAsc, including ties at the selection boundary.
// Requires a 256-thread block and a whole number of warps per shared row.
template <int N>
__device__ void BitonicSortAscWarp(float* values, int* indices) {
  static_assert(N >= 256 && (N & (N - 1)) == 0);
  for (int k = 2; k <= N; k <<= 1) {
    for (int j = k >> 1; j >= 32; j >>= 1) {
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
    for (int i = threadIdx.x; i < N; i += 256) {
      float value = values[i];
      int index = indices[i];
      for (int j = min(k >> 1, 16); j > 0; j >>= 1) {
        const float peer_value = __shfl_xor_sync(0xffffffff, value, j);
        const int peer_index = __shfl_xor_sync(0xffffffff, index, j);
        const bool keep_smaller = ((i & j) == 0) == ((i & k) == 0);
        if (keep_smaller ? value > peer_value : value < peer_value) {
          value = peer_value;
          index = peer_index;
        }
      }
      values[i] = value;
      indices[i] = index;
    }
    __syncthreads();
  }
}

__global__ void MergeChunkTopkWarpKernel(
    const float* __restrict__ logits_c, int block_off, int max_blocks,
    int block_topk, int T, float* __restrict__ run_val,
    int* __restrict__ run_idx) {
  const int t = blockIdx.x;
  if (t >= T) return;
  __shared__ float s_val[kMaxBlocks];
  __shared__ int s_idx[kMaxBlocks];
  __shared__ float m_val[2 * kMaxBlockTopk];
  __shared__ int m_idx[2 * kMaxBlockTopk];
  const float* lrow = logits_c + static_cast<size_t>(t) * max_blocks;
  for (int g = threadIdx.x; g < kMaxBlocks; g += blockDim.x) {
    s_val[g] = (g < max_blocks) ? lrow[g] : -1e30f;
    s_idx[g] = block_off + g;
  }
  __syncthreads();
  // Local top-block_topk of this chunk -> last block_topk after the sort.
  BitonicSortAscWarp<kMaxBlocks>(s_val, s_idx);
  // Merge running top-k (first half) with this chunk's top-k (second half).
  for (int i = threadIdx.x; i < block_topk; i += blockDim.x) {
    m_val[i] = run_val[static_cast<size_t>(t) * block_topk + i];
    m_idx[i] = run_idx[static_cast<size_t>(t) * block_topk + i];
    m_val[block_topk + i] = s_val[kMaxBlocks - block_topk + i];
    m_idx[block_topk + i] = s_idx[kMaxBlocks - block_topk + i];
  }
  __syncthreads();
  BitonicSortAscWarp<2 * kMaxBlockTopk>(m_val, m_idx);
  for (int i = threadIdx.x; i < block_topk; i += blockDim.x) {
    run_val[static_cast<size_t>(t) * block_topk + i] = m_val[block_topk + i];
    run_idx[static_cast<size_t>(t) * block_topk + i] = m_idx[block_topk + i];
  }
}

}  // namespace

void MergeStreamingTopk(const float* logits, int block_offset, int tokens,
                        float* running_values, int* running_indices,
                        cudaStream_t stream) {
  MergeChunkTopkWarpKernel<<<tokens, 256, 0, stream>>>(
      logits, block_offset, kMaxBlocks, kMaxBlockTopk, tokens, running_values,
      running_indices);
}

}  // namespace q4t::model
