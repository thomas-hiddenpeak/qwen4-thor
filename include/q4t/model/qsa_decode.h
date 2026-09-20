#pragma once

#include <cuda_runtime.h>

#include <cstddef>
#include <cstdint>

#include "q4t/status.h"

namespace q4t::model {
// Fixed T=1, nq=24, nkv=2, hd=256; page/selection ABI matches FullAttention.
Status QsaDecodeSplit(const uint16_t* q, const uint16_t* kv_cache,
                      const int* page_table, const int* topk,
                      const int* topk_len, uint16_t* out, const uint16_t* gate,
                      int max_topk, const int* seq_id, size_t kv_seq_stride,
                      int pt_seq_stride, cudaStream_t stream);

}  // namespace q4t::model
