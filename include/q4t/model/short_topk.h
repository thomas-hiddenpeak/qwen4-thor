#pragma once

#include <cuda_runtime.h>

#include "q4t/status.h"

namespace q4t::model {
// Fixed 2048 score slots and top-512 complete groups. Positions must belong
// to the short-context branch (at most 2048 complete groups per row).
// logits[tokens,2048], topk[tokens,max_topk], positions/topk_len[tokens].
Status SelectShortTopk(const float* logits, int* topk, int* topk_len,
                       const int* positions, int tokens, int compress,
                       int max_topk, cudaStream_t stream);
}  // namespace q4t::model
