#pragma once

#include <cuda_runtime.h>

#include <cstddef>
#include <cstdint>

#include "q4t/status.h"

namespace q4t::model {
// Fixed nq=24, nkv=2, hd=256; page/selection ABI matches FullAttention.
// T tokens: grid=(T, nkv, 4) — each block owns one (token, kv-head) and one
// 64-column output partition, so the per-token math is bit-identical to the
// T=1 decode (sparse attention is no longer a T-dependence source).
Status QsaDecodeSplit(const uint16_t* q, const uint16_t* kv_cache,
                      const int* page_table, const int* topk,
                      const int* topk_len, uint16_t* out, const uint16_t* gate,
                      int max_topk, int T, const int* seq_id,
                      size_t kv_seq_stride, int pt_seq_stride,
                      cudaStream_t stream);

}  // namespace q4t::model
