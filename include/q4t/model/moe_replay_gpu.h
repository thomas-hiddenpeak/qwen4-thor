// Linked only by the standalone diagnostic, never by q4t_model or serve.
#pragma once

#include "q4t/model/moe.h"
#include "q4t/model/moe_replay.h"

namespace q4t::model {

struct MoEReplayObservation {
  std::vector<uint16_t> logits;
  std::vector<int32_t> ids;
  std::vector<float> weights;
};

// Synchronous diagnostic completion boundary, including failures. Validates
// actual hidden bytes against execution.hidden before submission. A null record
// records normal selection. Model/weight identities are bound by the loader in
// the standalone driver; this low-level function cannot infer tensor provenance.
Status MoEReplayForward(
    const uint16_t* x, const quant::MoEWeightLayout& routed,
    const MoEExtraWeights& extra, uint16_t* y, void* workspace,
    size_t workspace_bytes, void* gemm_ws, size_t gemm_ws_bytes,
    cudaStream_t stream, const MoEReplayIdentity& execution,
    const MoEReplayRecord* record, MoEReplayObservation* observation,
    const MoEReplayCompatibility* compatibility = nullptr);

}  // namespace q4t::model
