// Compile the same forward body into a separate diagnostic object. Production
// compilation excludes every replay branch, kernel and allocation.
#define Q4T_MOE_REPLAY_DIAGNOSTIC
#include "../../src/model/moe.cu"

#include "q4t/model/moe_replay_gpu.h"
#include "q4t/trace/sha256.h"

#include <bit>

namespace q4t::model {

Status MoEReplayRouterContract() {
  constexpr int E = 512;
  constexpr int k = 3;
  constexpr int T = 4;
  const std::vector<int32_t> fixed = {511, 0, 5, 0, 511, 5,
                                     5, 511, 0, 511, 5, 0};
  std::vector<uint16_t> logits(T * E, 0);
  // Tie, adjacent BF16, unsorted slots, and finite large dynamic range.
  logits[E] = 0x3f80;
  logits[E + 511] = 0x3f81;
  logits[E + 5] = 0x3f7f;
  logits[2 * E + 5] = 0xc000;
  logits[2 * E + 511] = 0x4000;
  logits[3 * E + 511] = 0x42c8;
  logits[3 * E + 5] = 0xc2c8;
  struct Buffers {
    uint16_t* logits = nullptr;
    int32_t* fixed = nullptr;
    int32_t* ids = nullptr;
    float* weights = nullptr;
    ~Buffers() {
      cudaDeviceSynchronize();
      cudaFree(logits);
      cudaFree(fixed);
      cudaFree(ids);
      cudaFree(weights);
    }
  } buffers;
  if (cudaMalloc(&buffers.logits, logits.size() * 2) != cudaSuccess ||
      cudaMalloc(&buffers.fixed, fixed.size() * 4) != cudaSuccess ||
      cudaMalloc(&buffers.ids, fixed.size() * 4) != cudaSuccess ||
      cudaMalloc(&buffers.weights, fixed.size() * 4) != cudaSuccess) {
    return Status::Fail("router contract allocation");
  }
  std::vector<int32_t> ids(fixed.size());
  std::vector<float> weights(fixed.size()), first;
  double maximum_error = 0;
  for (int pass = 0; pass < 2; ++pass) {
    if (pass == 1) logits[511] = 0x4000;  // Same IDs, new current score.
    if (cudaMemcpy(buffers.logits, logits.data(), logits.size() * 2,
                   cudaMemcpyHostToDevice) != cudaSuccess ||
        cudaMemcpy(buffers.fixed, fixed.data(), fixed.size() * 4,
                   cudaMemcpyHostToDevice) != cudaSuccess) {
      return Status::Fail("router contract upload");
    }
    if (pass == 0) {
      RouterTopkKernel<<<T, kBlock>>>(buffers.logits, buffers.ids,
                                      buffers.weights, T, E, k);
      if (cudaGetLastError() != cudaSuccess ||
          cudaMemcpy(ids.data(), buffers.ids, ids.size() * 4,
                      cudaMemcpyDeviceToHost) != cudaSuccess) {
        return Status::Fail("normal router boundary execution");
      }
      if (ids[0] != 0 || ids[1] != 1 || ids[2] != 2 ||
          ids[3] != 511 || ids[4] != 0 || ids[5] != 5) {
        return Status::Fail("normal router tie/near-tie order");
      }
    }
    RouterReplayKernel<<<T, 1>>>(buffers.logits, buffers.fixed, buffers.ids,
                                 buffers.weights, E, k);
    if (cudaGetLastError() != cudaSuccess ||
        cudaMemcpy(ids.data(), buffers.ids, ids.size() * 4,
                   cudaMemcpyDeviceToHost) != cudaSuccess ||
        cudaMemcpy(weights.data(), buffers.weights, weights.size() * 4,
                   cudaMemcpyDeviceToHost) != cudaSuccess) {
      return Status::Fail("router contract execution");
    }
    if (ids != fixed) return Status::Fail("router slot order changed");
    for (int row = 0; row < T; ++row) {
      double values[k], sum = 0, maximum = -INFINITY;
      for (int slot = 0; slot < k; ++slot) {
        values[slot] = std::bit_cast<float>(
            uint32_t{logits[row * E + fixed[row * k + slot]]} << 16);
        maximum = std::max(maximum, values[slot]);
      }
      for (double& value : values) {
        value = std::exp(value - maximum);
        sum += value;
      }
      for (int slot = 0; slot < k; ++slot) {
        const double error = std::abs(weights[row * k + slot] -
                                       values[slot] / sum);
        if (!std::isfinite(error) || error > 2e-6) {
          return Status::Fail("router CPU reference mismatch");
        }
        maximum_error = std::max(maximum_error, error);
      }
    }
    if (pass == 0) first = weights;
  }
  if (!(weights[0] > first[0] && weights[1] < first[1])) {
    return Status::Fail("router ignored current logits");
  }
  std::printf("router_contract PASS rows=4 passes=2 cpu_max_abs=%.9g "
              "current_scores_changed=1 boundary_ids=0,511\n", maximum_error);
  return {};
}

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
