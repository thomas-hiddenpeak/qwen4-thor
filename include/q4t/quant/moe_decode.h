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
// identical per-row arithmetic as the T=1 decode path). T>1 defaults to the
// expert-grouped path (one M=M_e GEMM per active expert): in the production
// unique/rows ~= 0.687 distribution this reads each expert's weights once
// instead of M_e times and saves ~1.7-2 ms per verify forward (moe-ab2-1013).
// Q4T_MOE_DECODE_PERROW opts back to the per-row path (reference/debug).
// Per-row quantization is unchanged in both paths; the grouped path is
// bit-exact vs per-row (moe_decode_grouped_vs_perrow: max_ulp=0.00).
// ws follows MoEWorkspace::RequiredBytes(T, 10, 2560, 640).
Status MoEDeviceDecodeMulti(const uint16_t* x, const int32_t* expert_ids,
                            const float* router_w, float* y,
                            const MoEWeightLayout& weights,
                            const MoEWorkspace& ws, void* gemm_ws,
                            size_t gemm_ws_bytes, int T, int k,
                            cudaStream_t stream);

}  // namespace q4t::quant
