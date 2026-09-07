// BF16 routed-expert MoE forward for the MTP draft layer.
//
// The MTP decoder layer's MoE stores its 512 routed experts in BF16 (the main
// model uses NVFP4 W4A4). This is the BF16 analogue of
// q4t::quant::MoERoutedForward: same token-list orchestration (build per-expert
// token lists, one host sync for the counts, per-expert gather -> gate/up GEMM
// -> SwiGLU -> down GEMM -> router-weighted scatter-add), but the per-expert
// GEMMs are plain BF16 (q4t::model::Bf16Gemm) with no runtime activation
// quantization.
//
//   for each token t and each of its top-k routed experts e:
//     h     = x[t] * W_gu[e]^T            (BF16 GEMM, [1, 2*moe_is])
//     g     = h[:, :moe_is], u = h[:, moe_is:]
//     inter = silu(g) * u                 (SwiGLU, [1, moe_is])
//     out_e = inter * W_dn[e]^T           (BF16 GEMM, [1, hs])
//   y[t] = sum_e  r[t, e] * out_e         (router-weighted scatter-add)
//
// The shared expert (also BF16) is handled by the MTP layer, not here.
#pragma once

#include <cuda_runtime.h>

#include <cstdint>
#include <string>

#include "q4t/io/weight_loader.h"
#include "q4t/model/moe.h"
#include "q4t/status.h"

namespace q4t {
namespace mtp {

// Device weights of the MTP layer's 512 routed BF16 experts. The checkpoint
// stores them as two 3-D tensors (row-major, expert-major):
//   gate_up_proj : [E, 2*moe_is, hs]  (gate rows [0, moe_is), up rows
//                                       [moe_is, 2*moe_is) per expert)
//   down_proj    : [E, hs, moe_is]
// Expert e's gate/up slice is gu + e*(2*moe_is*hs); its down slice is
// down + e*(hs*moe_is).
struct MoeBf16Weights {
  int E = 0;  // num routed experts (512)
  int hs = 0;  // hidden_size (2560)
  int moe_is = 0;  // moe_intermediate_size (640)

  uint16_t* gu = nullptr;  // [E, 2*moe_is, hs]
  uint16_t* down = nullptr;  // [E, hs, moe_is]

  void Free();
};

// Load the MTP routed-expert BF16 weights from `loader` under
//   {prefix}.experts.gate_up_proj   ([E, 2*moe_is, hs])
//   {prefix}.experts.down_proj      ([E, hs, moe_is])
// e.g. prefix = "mtp.layers.0.mlp".
Status LoadMoeBf16(const io::WeightLoader& loader, const std::string& prefix,
                   int E, int hs, int moe_is, MoeBf16Weights* out,
                   cudaStream_t stream);

// Device bytes MoeBf16RoutedForward carves from its `workspace` argument for
// `M` tokens with top-`k` routing: compact [M*k, hs] bf16 + gu_out [M*k,
// 2*moe_is] bf16 + inter [M*k, moe_is] bf16 + dn_out [M*k, hs] bf16, plus the
// per-expert token lists [E, M] int32 and counts [E] int32.
size_t MoeBf16WorkspaceBytes(int M, int k, int hs, int moe_is, int E);

// Run the BF16 routed-expert MoE forward for the MTP layer.
//
//   x          : device row-major [M, hs] uint16 (BF16) token activations
//   expert_ids : device row-major [M, k] int32, top-k routed expert index per
//                token (0..E-1)
//   router_w   : device row-major [M, k] float, router weight (softmax prob)
//   y          : device row-major [M, hs] float32, output. MUST be zeroed
//                before the call (the scatter-add accumulates into it).
//   weights    : the MTP layer's routed-expert BF16 weights
//   workspace  : device scratch (>= ~1 MiB for M<=64)
//   gemm_ws    : separate cuBLASLt workspace (>= ~32 MiB)
//   stream     : CUDA stream
//
// M tokens, k = num_experts_per_tok. Returns a Status.
Status MoeBf16RoutedForward(const uint16_t* x, const int32_t* expert_ids,
                            const float* router_w, float* y,
                            const MoeBf16Weights& weights, void* workspace,
                            void* gemm_ws, size_t gemm_ws_bytes, int M, int k,
                            cudaStream_t stream);

// Run the full BF16 MoE forward for the MTP layer (router + top-k routed BF16
// experts + shared expert + gated combine). The BF16 analogue of
// q4t::model::MoEForward (which uses NVFP4 routed experts).
//
//   x        : device row-major [T, hs] uint16 (BF16) token activations
//   routed   : the MTP layer's routed BF16 expert weights
//   extra    : the MTP layer's BF16 router + shared-expert weights
//   y        : device row-major [T, hs] uint16 (BF16) output
//   k        : num_experts_per_tok (top-k)
//   workspace: device scratch (>= MoeBf16WorkspaceBytes(T, k, hs, moe_is, E))
//   gemm_ws  : separate cuBLASLt workspace (>= ~32 MiB)
//   stream   : CUDA stream
Status MoeBf16Forward(const uint16_t* x, const MoeBf16Weights& routed,
                      const model::MoEExtraWeights& extra, uint16_t* y, int T,
                      int k, void* workspace, size_t workspace_bytes,
                      void* gemm_ws, size_t gemm_ws_bytes, cudaStream_t stream);

}  // namespace mtp
}  // namespace q4t
