// Fixed-window exact selection for the long-context one-pass indexer.
#pragma once
#include <cuda_runtime.h>
#include "q4t/status.h"
namespace q4t::model {
// logits[tokens,groups]; each scratch values/IDs buffer has tokens*16384
// elements. Requires groups in (2048,65536], tokens in [1,4], top-512.
Status SelectDecodeTopk(const float* logits, int groups, int tokens,
                        const int* positions, int compress, int max_topk,
                        float* values0, int* indices0, float* values1,
                        int* indices1, int* topk, int* topk_len,
                        cudaStream_t stream);
}  // namespace q4t::model
