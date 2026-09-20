// Exact streaming top-k merge, isolated from the other attention kernels.
#include "q4t/model/streaming_topk.h"

#include "bitonic_topk.cuh"

namespace q4t::model {
namespace {

constexpr int kMaxBlocks = 2048;
constexpr int kMaxBlockTopk = 512;

using detail::BitonicSortAscWarp;

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
