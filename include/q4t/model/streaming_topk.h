// Streaming QSA top-k merge for the model's 2048-candidate / 512-top-k shape.
#ifndef Q4T_MODEL_STREAMING_TOPK_H_
#define Q4T_MODEL_STREAMING_TOPK_H_

#include <cuda_runtime.h>

namespace q4t::model {

// logits: [tokens, 2048], running scores/IDs: [tokens, 512]. Preserves the
// original bitonic network's equal-score ordering. Enqueued on stream.
void MergeStreamingTopk(const float* logits, int block_offset, int tokens,
                        float* running_values, int* running_indices,
                        cudaStream_t stream);

}  // namespace q4t::model
#endif  // Q4T_MODEL_STREAMING_TOPK_H_
