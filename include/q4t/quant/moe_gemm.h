// Grouped NVFP4 MoE routed-expert forward (W4A4).
//
// Computes the routed-expert contribution of one qwen4_exp MoE layer:
//   for each token t and each of its top-k routed experts e:
//     h = x[t] * W_gu[e]^T            (NVFP4 GEMM, [1, 2*moe_is])
//     g = h[:, :moe_is], u = h[:, moe_is:]
//     inter = silu(g) * u             (SwiGLU, [1, moe_is])
//     out_e = inter * W_dn[e]^T       (NVFP4 GEMM, [1, hs])
//   y[t] = sum_e  r[t, e] * out_e     (router-weighted scatter-add)
//
// The shared expert (BF16) is handled separately by the model layer; this
// function covers only the 512 routed NVFP4 experts.
//
// The per-expert weights come from MoEWeightLayout (moe_weights.h): the merged
// gate/up GEMM uses gu_packed_expert(e)/gu_sf_expert(e) with alpha
// gu_w_scale2[e]*input_scale[e]; the down GEMM uses dn_packed_expert(e)/
// dn_sf_expert(e) with alpha dn_w_scale2[e]*input_scale[e]. Activations are
// quantized at runtime (act_quant.h) using the per-expert input_scale.
//
// All work is enqueued on `stream`; the host only syncs once (to read the
// token->expert assignments for per-expert token counts).
#pragma once

#include <cuda_runtime.h>

#include "q4t/quant/moe_weights.h"
#include "q4t/status.h"

namespace q4t {
namespace quant {

// Device workspace layout for MoERoutedForward. All regions are carved from a
// single cudaMalloc'd buffer; sizes depend on (M, E, hs, moe_is).
struct MoEWorkspace {
  size_t compact_bytes = 0;  // [M*k, hs] bf16 gathered activations
  size_t a_packed_bytes = 0;  // [M*k, hs/2] e2m1
  size_t a_sf_bytes = 0;  // swizzled e4m3 (padded to 128-row atoms)
  size_t gu_out_bytes = 0;  // [M*k, 2*moe_is] f32 gate/up GEMM output
  size_t inter_bytes = 0;  // [M*k, moe_is] f32 SwiGLU output
  size_t dn_out_bytes = 0;  // [M*k, hs] f32 down GEMM output

  size_t TotalBytes() const {
    return compact_bytes + a_packed_bytes + a_sf_bytes + gu_out_bytes +
           inter_bytes + dn_out_bytes;
  }
  // Size of the workspace for the given dims (call before cudaMalloc).
  // Depends only on M*k (total routed slots), hs, and moe_is — not on E.
  static size_t RequiredBytes(int M, int k, int hs, int moe_is);

  uint8_t* compact;
  uint8_t* a_packed;
  uint8_t* a_sf;
  float* gu_out;
  float* inter;
  float* dn_out;
  // Carve the regions out of a buffer of at least RequiredBytes().
  void Init(uint8_t* base);
};

// Run the routed-expert MoE forward for one layer.
//
//   x          : device row-major [M, hs] uint16 (BF16) token activations
//   expert_ids : device row-major [M, k] int32, top-k routed expert index per
//                token (0..E-1)
//   router_w   : device row-major [M, k] float, router weight (softmax prob)
//                for each (token, expert)
//   y          : device row-major [M, hs] float32, output. MUST be zeroed
//                before the call (the kernel accumulates into it).
//   weights    : the layer's routed-expert NVFP4 weights
//   workspace  : device buffer of at least MoEWorkspace::RequiredBytes(...)
//   gemm_ws    : separate cuBLASLt workspace (>= ~32 MiB)
//   stream     : CUDA stream
//
// M tokens, k = num_experts_per_tok. Returns a Status.
Status MoERoutedForward(const uint16_t* x, const int32_t* expert_ids,
                        const float* router_w, float* y,
                        const MoEWeightLayout& weights, void* workspace,
                        void* gemm_ws, size_t gemm_ws_bytes, int M, int k,
                        cudaStream_t stream);

}  // namespace quant
}  // namespace q4t
