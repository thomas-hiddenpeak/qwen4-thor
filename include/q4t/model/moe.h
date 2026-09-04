// Complete MoE MLP for one qwen4_exp layer (router + top-k routed experts +
// shared expert + gated combine).
//
// The qwen4_exp MoE (every layer) is:
//   logits   = x @ gate_w^T                       [T, E]  (router, BF16)
//   top-k    = argtopk(logits, k) per token; router_w = softmax(top-k logits)
//   routed   = sum over top-k experts of router_w * expert(x)   (NVFP4 W4A4,
//              MoERoutedForward — quant/moe_gemm.h)
//   shared   = (silu(x @ Wg) * (x @ Wu)) @ Wd     [T, hs]  (BF16 SwiGLU)
//   gate     = sigmoid(x @ shared_expert_gate_w)  [T]
//   out      = routed + gate * shared             [T, hs]  (NO residual add;
//              the residual is handled by the surrounding Hyper-Connection
//              combine, see model/hyperconnection.h)
//
// The top-k softmax normalizes over the SELECTED k logits only (not all E),
// matching the Qwen MoE router (verified against qwen35-thor's
// moe_router_topk_kernel).
#pragma once

#include <cuda_runtime.h>

#include <cstdint>
#include <string>
#include <vector>

#include "q4t/io/weight_loader.h"
#include "q4t/quant/moe_weights.h"
#include "q4t/status.h"

namespace q4t {
namespace model {

// BF16 router + shared-expert weights of one MoE layer (the routed NVFP4
// experts live separately in quant::MoEWeightLayout). All row-major uint16.
struct MoEExtraWeights {
  int E = 0;  // num_experts (router output dim)
  int hs = 0;  // hidden_size
  int shared_is = 0;  // shared_expert_intermediate_size

  uint16_t* gate = nullptr;  // [E, hs] router
  // Merged shared-expert gate/up: gate rows [0, shared_is), up rows
  // [shared_is, 2*shared_is). One [2*shared_is, hs] GEMM (gate then up).
  uint16_t* shared_gu = nullptr;  // [2*shared_is, hs]
  uint16_t* shared_down = nullptr;  // [hs, shared_is]
  uint16_t* shared_gate_scalar = nullptr;  // [1, hs]

  void Free();
};

// Load the BF16 router + shared-expert weights from `loader` under
//   {prefix}.gate.weight
//   {prefix}.shared_expert.gate_proj.weight   (gate rows of shared_gu)
//   {prefix}.shared_expert.up_proj.weight     (up rows of shared_gu)
//   {prefix}.shared_expert.down_proj.weight
//   {prefix}.shared_expert_gate.weight
// e.g. prefix = "model.language_model.layers.2.mlp". The shared gate/up are
// merged into one [2*shared_is, hs] buffer (gate rows first).
Status LoadMoEExtra(const io::WeightLoader& loader, const std::string& prefix,
                    int E, int hs, int shared_is, MoEExtraWeights* out,
                    cudaStream_t stream);

// Device bytes required for the MoEForward `workspace` argument (the routed
// MoEWorkspace plus the router/shared-expert scratch). `moe_is` comes from
// the routed MoEWeightLayout.
size_t MoEForwardWorkspaceBytes(int T, int k, int hs, int moe_is, int shared_is,
                                int E);

// Run the full MoE forward for one layer.
//
//   x        : device row-major [T, hs] uint16 (BF16) token activations
//   routed   : the layer's routed NVFP4 expert weights
//   extra    : the layer's BF16 router + shared-expert weights
//   y        : device row-major [T, hs] uint16 (BF16) output
//   k        : num_experts_per_tok (top-k)
//   workspace: device scratch (>= ~1 MiB for T<=64)
//   gemm_ws  : separate cuBLASLt workspace (>= ~32 MiB), shared by the BF16
//              GEMMs and MoERoutedForward
//   stream   : CUDA stream
Status MoEForward(const uint16_t* x, const quant::MoEWeightLayout& routed,
                  const MoEExtraWeights& extra, uint16_t* y, int T, int k,
                  void* workspace, size_t workspace_bytes, void* gemm_ws,
                  size_t gemm_ws_bytes, cudaStream_t stream);

}  // namespace model
}  // namespace q4t
