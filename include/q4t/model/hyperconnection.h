// Hyper-Connection (GatedResidual) — the qwen4_exp backbone residual.
//
// The trunk is NOT a plain residual: each token's hidden state is `hc_count`
// (4) branches of `hidden_size` (2560) = 10240 dims. Each decoder layer has an
// `attn_hyper_connection` and an `mlp_hyper_connection`, both GatedResidual
// (use_mix + use_combine); the model end has a `hyper_connection_mixer`
// (GatedResidual, use_combine=False) that mixes the 4 branches to 2560.
//
// Math (mirrors SGLang sglang/srt/layers/hyperconnection.py, torch path):
//   mix(hyper_input [T, hc*hs]):
//     normed = GroupedGemmaRMSNorm(hyper_input)         // per-branch, (1+w)
//     gate   = sigmoid( W_up @ silu( W_down @ normed / hc ) )   // [T, hc*hs]
//     mixed  = (gate * normed).view(T, hc, hs).mean(-2)          // [T, hs]
//     return (mixed, (hyper_input, normed))
//   combine(block_output [T, hs], residual=(hyper_input, normed)):
//     inject = 2 * sigmoid( W_inject @ normed / hc )             // [T, hc]
//     return ( R.view(T,hc,hs) + block_output.unsqueeze(1) *
//              inject.unsqueeze(-1) ).flatten                     // [T, hc*hs]
//
// GroupedGemmaRMSNorm with hc_per_branch_norm=true: the 10240-dim vector is
// split into hc_count groups of hidden_size; each group is RMSNorm'd
// independently, then scaled by (1 + weight) (weight is the checkpoint
// `hc_norm.weight`, zero-initialized).
#pragma once

#include <cuda_runtime.h>

#include <cstdint>
#include <vector>

#include "q4t/io/weight_loader.h"
#include "q4t/model/gemv.h"
#include "q4t/status.h"

namespace q4t {
namespace model {

// Device weights of one GatedResidual module. All BF16 (uint16), row-major.
// `block_inject` is null when use_combine=false (the mixer).
struct HyperConnectionWeights {
  int hc_count = 4;
  int hidden_size = 2560;
  int lowrank = 320;
  float eps = 1e-6f;
  bool use_combine = true;

  uint16_t* hc_norm = nullptr;  // [hc*hs]
  uint16_t* mix_down = nullptr;  // [lowrank, hc*hs]
  uint16_t* mix_up = nullptr;  // [hc*hs, lowrank]
  uint16_t* block_inject = nullptr;  // [hc, hc*hs] (null if !use_combine)

  // FP8 (e4m3) decode shadows of the low-rank mix projections (built at load
  // when Q4T_FP8_PROJ / Q4T_FP8_HC is on). block_inject (N=hc=4) stays BF16.
  Fp8Shadow mix_down_fp8;
  Fp8Shadow mix_up_fp8;

  int hc_dim() const { return hc_count * hidden_size; }
  void Free();
};

// Load one GatedResidual's weights from `loader` under the checkpoint names
//   {prefix}.hc_norm.weight
//   {prefix}.input_mix_weight_down.weight
//   {prefix}.input_mix_weight_up.weight
//   {prefix}.block_inject_weight.weight   (only if use_combine)
// e.g. prefix = "model.language_model.layers.0.attn_hyper_connection".
Status LoadHyperConnection(const io::WeightLoader& loader, const std::string& prefix,
                           int hc_count, int hidden_size, int lowrank, float eps,
                           bool use_combine, HyperConnectionWeights* out,
                           cudaStream_t stream);

// Optional caller-owned storage. Both buffers must remain valid until the
// enqueued mix completes, and must not overlap normed, mixed or GEMM scratch.
struct HyperConnectionMixScratch {
  uint16_t* down = nullptr;
  uint16_t* up = nullptr;
  size_t down_bytes = 0;
  size_t up_bytes = 0;
};

// Run mix: hyper_input [T, hc*hs] BF16 -> mixed [T, hs] BF16, and the
// (hyper_input, normed) residual pair for the later combine.
//
//   hyper_input : device row-major [T, hc*hs] uint16 (BF16)
//   mixed       : device row-major [T, hs] uint16 (out)
//   normed      : device row-major [T, hc*hs] uint16 (out, for combine)
//   workspace   : scratch device buffer (>= ~32 MiB) for the two low-rank GEMMs
Status HyperConnectionMix(
    const HyperConnectionWeights& w, const uint16_t* hyper_input,
    uint16_t* mixed, uint16_t* normed, int T, void* workspace,
    size_t workspace_bytes, cudaStream_t stream,
    const HyperConnectionMixScratch* mix_scratch = nullptr);

// Optional caller-owned gate storage. It must remain valid through the last
// enqueued GRWrite consumer (or pending Read work on an abandoned frame),
// and must not overlap residual, mix scratch or any sublayer workspace.
struct GatedResidualGateStorage {
  uint16_t* data = nullptr;
  size_t bytes = 0;
};

// Ephemeral GRRead -> sublayer -> GRWrite contract. Borrows the residual and
// optionally the gate storage; otherwise owns its gate allocation. Normed
// scratch is not retained. The stream must outlive this frame.
class GatedResidualFrame {
 public:
  GatedResidualFrame() = default;
  ~GatedResidualFrame();
  GatedResidualFrame(const GatedResidualFrame&) = delete;
  GatedResidualFrame& operator=(const GatedResidualFrame&) = delete;

  Status Read(const HyperConnectionWeights& w, const uint16_t* residual,
              uint16_t* mixed, uint16_t* normed_scratch, int tokens,
              void* workspace, size_t workspace_bytes, cudaStream_t stream,
              const HyperConnectionMixScratch* mix_scratch = nullptr,
              const GatedResidualGateStorage* gate_storage = nullptr);
  // Consumes the frame; all block-output producers must join the Read stream.
  Status Write(const uint16_t* block_output, uint16_t* output);

 private:
  cudaError_t Release();
  const uint16_t* residual_ = nullptr;
  uint16_t* inject_gate_ = nullptr;
  cudaStream_t stream_ = nullptr;
  int tokens_ = 0;
  int hc_ = 0;
  int hs_ = 0;
  bool ready_ = false;
  bool owns_gate_ = false;
};

// Run combine: block_output [T, hs] + residual (hyper_input, normed) [T, hc*hs]
// -> updated residual [T, hc*hs] BF16.
Status HyperConnectionCombine(const HyperConnectionWeights& w,
                              const uint16_t* block_output,
                              const uint16_t* hyper_input, const uint16_t* normed,
                              uint16_t* out, int T, void* workspace,
                              size_t workspace_bytes, cudaStream_t stream);

}  // namespace model
}  // namespace q4t
