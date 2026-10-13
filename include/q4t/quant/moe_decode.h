// Fixed-shape device-routed NVFP4 MoE. See dataflow-engine/MOE_DEVICE_PLAN.md.
#pragma once

#include "q4t/quant/moe_gemm.h"

namespace q4t::quant {

// Requires M=1, E=512, k=10, hs=2560, moe_is=640 and valid unique router IDs.
// Enqueues the complete routed chain on stream without reading routing to host.
// ws follows MoEWorkspace::RequiredBytes(1, 10, 2560, 640).
Status MoEDeviceDecode(const uint16_t* x, const int32_t* expert_ids,
                       const float* router_w, float* y,
                       const MoEWeightLayout& weights, const MoEWorkspace& ws,
                       void* gemm_ws, size_t gemm_ws_bytes,
                       cudaStream_t stream);

// 2 <= T <= 8 batched device decode (MTP verify / batched decode). Same
// fixed shapes as MoEDeviceDecode (E=512, k=10, hs=2560, moe_is=640) and the
// same unique-router-ID requirement. No host routing readback, no per-expert
// GEMM loop.
//
// T=1 keeps the per-row path bit-identically (R = T*k independent M=1 rows,
// identical per-row arithmetic as the T=1 decode path). T>1 also defaults to
// the per-row path: production sub-timing + moe_gemm_grouped_bench show the
// per-row grouped GEMM already reuses duplicate-expert weights from L2, so
// expert grouping (one M=M_e GEMM per active expert, opt-in via
// Q4T_MOE_DECODE_GROUPED) buys only 3-7% on the GEMM in the production
// unique/rows ~= 0.687 distribution. Per-row quantization is unchanged in
// both paths; the grouped path's M_e tiling stays within the near-tie ULP
// contract (moe_decode_grouped_vs_perrow: bit-exact).
// ws follows MoEWorkspace::RequiredBytes(T, 10, 2560, 640).
Status MoEDeviceDecodeMulti(const uint16_t* x, const int32_t* expert_ids,
                            const float* router_w, float* y,
                            const MoEWeightLayout& weights,
                            const MoEWorkspace& ws, void* gemm_ws,
                            size_t gemm_ws_bytes, int T, int k,
                            cudaStream_t stream);

}  // namespace q4t::quant
