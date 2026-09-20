#include "q4t/model/decode_topk.h"

#include "bitonic_topk.cuh"

namespace q4t::model {
namespace {
using f32 = float;
constexpr int kMaxBlocks = 2048;
constexpr int kTopk = 512;
__global__ void DecodeSliceLocalTopkKernel(const f32* __restrict__ logits,
                                           int n_groups, int max_blocks,
                                           int block_topk, int T,
                                           f32* __restrict__ out_val,
                                           int* __restrict__ out_idx,
                                           int stride) {
  const int t = blockIdx.y;
  if (t >= T) return;
  const int base = blockIdx.x * max_blocks;
  __shared__ float s_val[kMaxBlocks];
  __shared__ int s_idx[kMaxBlocks];
  const f32* lrow = logits + static_cast<size_t>(t) * n_groups;
  for (int g = threadIdx.x; g < kMaxBlocks; g += blockDim.x) {
    const int gg = base + g;
    s_val[g] = (gg < n_groups) ? lrow[gg] : -1e30f;
    s_idx[g] = gg;
  }
  __syncthreads();
  detail::BitonicSortAscWarp<kMaxBlocks>(s_val, s_idx);
  __syncthreads();
  for (int i = threadIdx.x; i < block_topk; i += blockDim.x) {
    const size_t o =
        static_cast<size_t>(t) * stride + blockIdx.x * block_topk + i;
    out_val[o] = s_val[kMaxBlocks - block_topk + i];
    out_idx[o] = s_idx[kMaxBlocks - block_topk + i];
  }
}

// Merge level: local top-block_topk of each 2048-window of the current
// candidate list (val,idx). grid = (ceil(C/max_blocks), T). Reads C candidates
// from in (stride), writes ceil(C/max_blocks)*block_topk to out (stride).
__global__ void DecodeWindowMergeTopkKernel(
    const f32* __restrict__ in_val, const int* __restrict__ in_idx, int C,
    int in_stride, int max_blocks, int block_topk, int T,
    f32* __restrict__ out_val, int* __restrict__ out_idx, int out_stride) {
  const int t = blockIdx.y;
  if (t >= T) return;
  const int base = blockIdx.x * max_blocks;
  __shared__ float s_val[kMaxBlocks];
  __shared__ int s_idx[kMaxBlocks];
  const f32* iv = in_val + static_cast<size_t>(t) * in_stride;
  const int* ii = in_idx + static_cast<size_t>(t) * in_stride;
  for (int g = threadIdx.x; g < kMaxBlocks; g += blockDim.x) {
    const int k = base + g;
    s_val[g] = (k < C) ? iv[k] : -1e30f;
    s_idx[g] = (k < C) ? ii[k] : -1;
  }
  __syncthreads();
  detail::BitonicSortAscWarp<kMaxBlocks>(s_val, s_idx);
  __syncthreads();
  for (int i = threadIdx.x; i < block_topk; i += blockDim.x) {
    const size_t o =
        static_cast<size_t>(t) * out_stride + blockIdx.x * block_topk + i;
    out_val[o] = s_val[kMaxBlocks - block_topk + i];
    out_idx[o] = s_idx[kMaxBlocks - block_topk + i];
  }
}

// Final: one block per token. Sort the (<= 2048) surviving candidates, take
// top-block_topk, expand to token positions + force-include the current group
// tail. Mirrors ExpandRunTopkKernel but reads the one-pass candidate list.
__global__ void DecodeFinalTopkExpandKernel(
    const f32* __restrict__ cand_val, const int* __restrict__ cand_idx, int C,
    int stride, const int* __restrict__ positions, int T, int compress,
    int block_topk, int* __restrict__ topk, int* __restrict__ topk_len,
    int max_topk) {
  const int t = blockIdx.x;
  if (t >= T) return;
  const int pos = positions[t];
  __shared__ float s_val[kMaxBlocks];
  __shared__ int s_idx[kMaxBlocks];
  const f32* cv = cand_val + static_cast<size_t>(t) * stride;
  const int* ci = cand_idx + static_cast<size_t>(t) * stride;
  for (int g = threadIdx.x; g < kMaxBlocks; g += blockDim.x) {
    s_val[g] = (g < C) ? cv[g] : -1e30f;
    s_idx[g] = (g < C) ? ci[g] : -1;
  }
  __syncthreads();
  detail::BitonicSortAscWarp<kMaxBlocks>(s_val, s_idx);
  __syncthreads();
  int* out = topk + static_cast<size_t>(t) * max_topk;
  int valid_count = 0;
  for (int base = kMaxBlocks - block_topk; base < kMaxBlocks;
       base += blockDim.x) {
    const int c = base + threadIdx.x;
    const bool valid = c < kMaxBlocks && s_val[c] > -1e29f;
    valid_count += __syncthreads_count(valid);
  }
  const int first = kMaxBlocks - valid_count;
  for (int c = first + threadIdx.x; c < kMaxBlocks; c += blockDim.x) {
    const int base = s_idx[c] * compress;
    for (int j = 0; j < compress; ++j)
      out[(c - first) * compress + j] = base + j;
  }
  int n = (kMaxBlocks - first) * compress;
  const int tail = (pos + 1) % compress;
  if (threadIdx.x < tail) out[n + threadIdx.x] = pos + 1 - tail + threadIdx.x;
  n += tail;
  for (int i = n + threadIdx.x; i < max_topk; i += blockDim.x) out[i] = -1;
  if (threadIdx.x == 0) topk_len[t] = n;
}

}  // namespace
Status SelectDecodeTopk(const float* logits, int groups, int tokens,
                        const int* positions, int compress, int max_topk,
                        float* values0, int* indices0, float* values1,
                        int* indices1, int* topk, int* topk_len,
                        cudaStream_t stream) {
  if (groups <= kMaxBlocks || groups > 65536 || tokens < 1 || tokens > 4 ||
      compress <= 0 || max_topk < kTopk * compress + compress - 1) {
    return Status::Fail("unsupported decode top-k shape");
  }
  const int slices = (groups + kMaxBlocks - 1) / kMaxBlocks;
  int current = slices * kTopk;
  DecodeSliceLocalTopkKernel<<<dim3(slices, tokens), 256, 0, stream>>>(
      logits, groups, kMaxBlocks, kTopk, tokens, values0, indices0, current);
  float* in_v = values0;
  int* in_i = indices0;
  float* out_v = values1;
  int* out_i = indices1;
  int stride = current;
  while (current > kMaxBlocks) {
    const int windows = (current + kMaxBlocks - 1) / kMaxBlocks;
    const int next = windows * kTopk;
    DecodeWindowMergeTopkKernel<<<dim3(windows, tokens), 256, 0, stream>>>(
        in_v, in_i, current, stride, kMaxBlocks, kTopk, tokens, out_v, out_i,
        next);
    float* v = in_v;
    in_v = out_v;
    out_v = v;
    int* i = in_i;
    in_i = out_i;
    out_i = i;
    stride = next;
    current = next;
  }
  DecodeFinalTopkExpandKernel<<<tokens, 256, 0, stream>>>(
      in_v, in_i, current, stride, positions, tokens, compress, kTopk, topk,
      topk_len, max_topk);
  const cudaError_t error = cudaGetLastError();
  if (error != cudaSuccess) return Status::Fail(cudaGetErrorString(error));
  return {};
}
}  // namespace q4t::model
