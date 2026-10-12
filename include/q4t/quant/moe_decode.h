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
// same unique-router-ID requirement. R = T*k rows, each (token, slot) an
// independent M=1 NVFP4 GEMM row, so every token's expert output uses the
// identical per-row arithmetic as the T=1 decode path. No host routing
// readback, no per-expert GEMM loop. ws follows
// MoEWorkspace::RequiredBytes(T, 10, 2560, 640).
Status MoEDeviceDecodeMulti(const uint16_t* x, const int32_t* expert_ids,
                            const float* router_w, float* y,
                            const MoEWeightLayout& weights,
                            const MoEWorkspace& ws, void* gemm_ws,
                            size_t gemm_ws_bytes, int T, int k,
                            cudaStream_t stream);

}  // namespace q4t::quant
