#include "q4t/model/indexer_decode.h"

#include <cmath>
#include <cstring>

namespace q4t::model {
namespace {
using u16 = uint16_t;
using f32 = float;
__device__ __forceinline__ f32 Bf16ToFloat(u16 x) {
  uint32_t bits = static_cast<uint32_t>(x) << 16;
  float f;
  std::memcpy(&f, &bits, sizeof(f));
  return f;
}
__global__ void IndexerDecodeLogitsKernel(
    const u16* __restrict__ iq, const u16* __restrict__ ck,
    f32* __restrict__ logits, const int* __restrict__ positions, int T,
    int n_iq, int hd, int compress, int max_blocks, int block_off,
    const int* __restrict__ d_seq_id, size_t idx_seq_stride) {
  int t = blockIdx.y;
  if (t >= T) return;
  const int pos = positions[t];
  const int n_groups =
      (pos + 1) / compress;  // global visible groups (uncapped)
  const float inv_sqrt = 1.f / sqrtf(static_cast<float>(hd));
  const u16* cks =
      d_seq_id ? ck + static_cast<size_t>(d_seq_id[t]) * idx_seq_stride : ck;
  // Scores the chunk of GLOBAL blocks [block_off, block_off + max_blocks);
  // logits written at the local column lg = g - block_off.
  for (int lg = blockIdx.x * blockDim.x + threadIdx.x; lg < max_blocks;
       lg += gridDim.x * blockDim.x) {
    const int g = block_off + lg;
    float val = -1e30f;
    if (g < n_groups) {
      float sum = 0.f;
      for (int h = 0; h < n_iq; ++h) {
        float dot = 0.f;
        for (int d = 0; d < hd; ++d)
          dot += Bf16ToFloat(iq[(static_cast<size_t>(t) * n_iq + h) * hd + d]) *
                 Bf16ToFloat(cks[static_cast<size_t>(g) * hd + d]);
        sum += fmaxf(dot, 0.f);
      }
      val = sum * inv_sqrt;
    }
    logits[static_cast<size_t>(t) * max_blocks + lg] = val;
  }
}
}  // namespace

Status IndexerDecodeScores(const uint16_t* iq, const uint16_t* ck,
                           float* logits, const int* positions, int compress,
                           const int* seq_id, size_t idx_seq_stride,
                           cudaStream_t stream) {
  if (compress <= 0 || seq_id == nullptr) {
    return Status::Fail("unsupported indexer decode shape");
  }
  IndexerDecodeLogitsKernel<<<dim3(8, 1), 256, 0, stream>>>(
      iq, ck, logits, positions, 1, 4, 128, compress, 2048, 0, seq_id,
      idx_seq_stride);
  const cudaError_t error = cudaGetLastError();
  if (error != cudaSuccess) return Status::Fail(cudaGetErrorString(error));
  return {};
}
}  // namespace q4t::model
