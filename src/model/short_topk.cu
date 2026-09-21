#include "q4t/model/short_topk.h"

#include "bitonic_topk.cuh"

namespace q4t::model {
namespace {
using f32 = float;
__global__ void ShortTopkSelectKernel(const f32* __restrict__ logits,
                                      int* __restrict__ topk,
                                      int* __restrict__ topk_len,
                                      const int* __restrict__ positions, int T,
                                      int compress, int block_topk,
                                      int max_blocks, int max_topk) {
  __shared__ float s_val[2048];  // kMaxBlocks
  __shared__ int s_idx[2048];
  const int t = blockIdx.x;
  if (t >= T) return;
  const int pos = positions[t];
  const int n_groups =
      (pos + 1) / compress > max_blocks ? max_blocks : (pos + 1) / compress;
  int* out = topk + static_cast<size_t>(t) * max_topk;
  const f32* lrow = logits + static_cast<size_t>(t) * max_blocks;
  int n;
  if (n_groups <= block_topk) {
    // dense: all visible positions (QSA == dense causal attention)
    for (int p = threadIdx.x; p <= pos; p += 256) out[p] = p;
    n = pos + 1;
  } else {
    // sparse: load (logit, block index) pairs; pad with -inf so padding
    // sorts to the front (ascending) and is never among the top block_topk.
    for (int g = threadIdx.x; g < 2048; g += 256) {
      s_val[g] = (g < n_groups) ? lrow[g] : -1e30f;
      s_idx[g] = g;
    }
    __syncthreads();
    detail::BitonicSortAscWarp<2048>(s_val, s_idx);
    // Expand in sorted-block order. Atomic slot allocation changes the
    // floating-point reduction order in SparseAttentionKernel across runs.
    for (int c = threadIdx.x; c < block_topk; c += 256) {
      const int g = s_idx[2048 - block_topk + c];
      const int base = g * compress;
      for (int j = 0; j < compress; ++j) {
        const int p = base + j;
        out[c * compress + j] = p;
      }
    }
    // Complete groups are governed solely by top-k selection. Only the
    // unfinished group's visible tail is appended (at most compress - 1).
    const int tail = (pos + 1) % compress;
    n = block_topk * compress;
    if (threadIdx.x < tail) out[n + threadIdx.x] = pos + 1 - tail + threadIdx.x;
    n += tail;
  }
  for (int i = n + threadIdx.x; i < max_topk; i += 256) out[i] = -1;
  if (threadIdx.x == 0) topk_len[t] = n;
}

}  // namespace

Status SelectShortTopk(const float* logits, int* topk, int* topk_len,
                       const int* positions, int tokens, int compress,
                       int max_topk, cudaStream_t stream) {
  if (tokens < 1 || tokens > 8192 || compress <= 0 ||
      max_topk < 512 * compress + compress - 1) {
    return Status::Fail("unsupported short top-k shape");
  }
  ShortTopkSelectKernel<<<tokens, 256, 0, stream>>>(
      logits, topk, topk_len, positions, tokens, compress, 512, 2048, max_topk);
  const cudaError_t error = cudaGetLastError();
  if (error != cudaSuccess) return Status::Fail(cudaGetErrorString(error));
  return {};
}
}  // namespace q4t::model
