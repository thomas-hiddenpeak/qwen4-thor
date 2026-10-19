// Compile the same forward body into a separate diagnostic object. Production
// compilation excludes every replay branch, kernel and allocation.
#define Q4T_MOE_REPLAY_DIAGNOSTIC
#include "../../src/model/moe.cu"

#include "q4t/model/moe_replay_gpu.h"
#include "q4t/trace/sha256.h"

namespace q4t::model {

Status MoEReplayForward(
    const uint16_t* x, const quant::MoEWeightLayout& routed,
    const MoEExtraWeights& extra, uint16_t* y, void* workspace,
    size_t workspace_bytes, void* gemm_ws, size_t gemm_ws_bytes,
    cudaStream_t stream, const MoEReplayIdentity& execution,
    const MoEReplayRecord* record, MoEReplayObservation* observation,
    const MoEReplayCompatibility* compatibility) {
  if (!observation) return Status::Fail("replay: missing observation");
  *observation = {};
  // RAII drains even early failures. Storage is declared before the guard so
  // all asynchronous host reads finish before its storage can be destroyed.
  MoEReplayRecord snapshot;
  std::vector<uint16_t> hidden;
  struct Drain {
    cudaStream_t stream;
    int32_t* ids = nullptr;
    ~Drain() {
      cudaStreamSynchronize(stream);
      if (ids) cudaFree(ids);
    }
  } drain{stream};
  const int T = execution.rows;
  const int k = execution.top_k;
  if (!x || !y || !workspace || T <= 0 || k <= 0 || k > 16 ||
      execution.experts != extra.E || extra.E > 512 || extra.hs <= 0 ||
      routed.E != extra.E || routed.hs != extra.hs || routed.moe_is <= 0 ||
      routed.slot_mode) {
    return Status::Fail("replay: invalid execution buffers/shape");
  }
  std::vector<int32_t> normal_ids;
  if (!record) {
    normal_ids.resize(static_cast<size_t>(T) * k);
    for (size_t i = 0; i < normal_ids.size(); ++i) normal_ids[i] = i % k;
  }
  auto status = snapshot.Prepare(record ? record->source() : execution,
                                 execution,
                                 record ? record->ids() : normal_ids,
                                 compatibility);
  if (!status.ok()) return status;
  hidden.resize(static_cast<size_t>(T) * extra.hs);
  if (cudaMemcpyAsync(hidden.data(), x, hidden.size() * sizeof(uint16_t),
                      cudaMemcpyDeviceToHost, stream) != cudaSuccess ||
      cudaStreamSynchronize(stream) != cudaSuccess) {
    return Status::Fail("replay: hidden read failed");
  }
  if (trace::Sha256({reinterpret_cast<const uint8_t*>(hidden.data()),
                     hidden.size() * sizeof(uint16_t)}) != execution.hidden) {
    return Status::Fail("replay: actual hidden identity mismatch");
  }
  if (record) {
    const size_t bytes = snapshot.ids().size_bytes();
    if (cudaMalloc(reinterpret_cast<void**>(&drain.ids), bytes) != cudaSuccess ||
        cudaMemcpyAsync(drain.ids, snapshot.ids().data(), bytes,
                        cudaMemcpyHostToDevice, stream) != cudaSuccess) {
      return Status::Fail("replay: ID upload failed");
    }
  }
  status = MoEForwardReplayImpl(x, routed, extra, y, T, k, workspace,
                                workspace_bytes, gemm_ws, gemm_ws_bytes, stream,
                                nullptr, execution.layer, nullptr, nullptr,
                                drain.ids);
  if (!status.ok()) return status;
  const size_t routed_bytes = quant::MoEWorkspace::RequiredBytes(
      T, k, extra.hs, routed.moe_is);
  const uint8_t* scratch = static_cast<const uint8_t*>(workspace) +
                           ((routed_bytes + 7) & ~size_t(7));
  observation->logits.resize(static_cast<size_t>(T) * extra.E);
  observation->ids.resize(static_cast<size_t>(T) * k);
  observation->weights.resize(static_cast<size_t>(T) * k);
  const auto copy = [&scratch, stream](auto& values) {
    const size_t bytes = values.size() * sizeof(values[0]);
    const auto result = cudaMemcpyAsync(values.data(), scratch, bytes,
                                        cudaMemcpyDeviceToHost, stream);
    scratch += bytes;
    return result == cudaSuccess;
  };
  if (!copy(observation->logits) || !copy(observation->ids) ||
      !copy(observation->weights) ||
      cudaStreamSynchronize(stream) != cudaSuccess) {
    return Status::Fail("replay: observation completion failed");
  }
  return {};
}

}  // namespace q4t::model
