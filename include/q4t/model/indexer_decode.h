#pragma once

#include <cuda_runtime.h>

#include <cstddef>
#include <cstdint>

#include "q4t/status.h"

namespace q4t::model {
// Fixed T=1, n_iq=4, hd=128, 2048 score slots, block offset zero.
// seq_id selects a compressed-key slice; no new storage or synchronization.
Status IndexerDecodeScores(const uint16_t* iq, const uint16_t* ck,
                           float* logits, const int* positions, int compress,
                           const int* seq_id, size_t idx_seq_stride,
                           cudaStream_t stream);
}  // namespace q4t::model
