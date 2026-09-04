// NVFP4 routed-expert MoE weight layout + direct-to-packed loader.
//
// The qwen4_exp checkpoint stores each of the 512 routed experts per layer as
// three NVFP4 projections:
//   gate_proj : weight U8 [moe_is, hs/2],  weight_scale F8_E4M3 [moe_is, hs/16]
//   up_proj   : weight U8 [moe_is, hs/2],  weight_scale F8_E4M3 [moe_is, hs/16]
//   down_proj : weight U8 [hs, moe_is/2],  weight_scale F8_E4M3 [hs, moe_is/16]
// plus FP32 scalars weight_scale_2 (inv global scale) and input_scale.
//
// For a given expert, gate_proj and up_proj share identical weight_scale_2 and
// input_scale (verified on the real checkpoint), so they are merged into one
// [2*moe_is, hs] GEMM (gate rows first, then up rows) with a single alpha.
// down_proj is separate ([hs, moe_is]).
//
// The loader pre-allocates four big device buffers per layer and H2D-copies
// each expert's tensors directly to its offset (skipping ~73K per-tensor
// cudaMallocs, mirroring qwen35-thor's direct-to-packed). The weight_scale
// tensors are swizzled on the host (SwizzleSf) into the 128x64 atom layout
// that cuBLASLt VEC16_UE4M3 requires; the packed weights stay row-major.
#pragma once

#include <cuda_runtime.h>

#include <cstdint>
#include <string>
#include <vector>

#include "q4t/io/weight_loader.h"
#include "q4t/quant/swizzle.h"
#include "q4t/status.h"

namespace q4t {
namespace quant {

// Device layout of one layer's routed-expert NVFP4 weights.
//
// The scale-factor (SF) buffers are stored as E independent swizzled blocks
// (one per expert) rather than one globally-swizzled buffer, because the
// 128-row swizzle atom means a per-expert slice of a globally-swizzled buffer
// is not addressable. Since 2*moe_is (1280) and hs (2560) are both multiples
// of 128, each expert's SF is a valid standalone swizzled buffer, so a
// per-expert GEMM can address gu_sf_expert(e) / dn_sf_expert(e) directly.
struct MoEWeightLayout {
  int E = 0;  // num_experts
  int hs = 0;  // hidden_size (K of gate/up, N of down)
  int moe_is = 0;  // moe_intermediate_size (N of gate/up, K of down)

  // Merged gate/up: gate rows [0, moe_is), up rows [moe_is, 2*moe_is) per
  // expert. Packed is one contiguous [2*E*moe_is, hs/2] row-major buffer.
  uint8_t* gu_packed = nullptr;  // [2*E*moe_is, hs/2] row-major e2m1
  uint8_t* gu_sf = nullptr;      // E blocks of SfBufferSize(2*moe_is, hs)
  // down:
  uint8_t* dn_packed = nullptr;  // [E*hs, moe_is/2] row-major e2m1
  uint8_t* dn_sf = nullptr;      // E blocks of SfBufferSize(hs, moe_is)

  // Per-expert FP32 global scales (= checkpoint weight_scale_2, the inverse
  // global scale) and activation scales (= checkpoint input_scale), on device.
  float* gu_w_scale2 = nullptr;  // [E]
  float* gu_input_scale = nullptr;  // [E]
  float* dn_w_scale2 = nullptr;  // [E]
  float* dn_input_scale = nullptr;  // [E]

  // Host copies of the per-expert scales (so the GEMM wrappers can compute
  // alpha without a D2H sync).
  std::vector<float> gu_w_scale2_h;  // [E]
  std::vector<float> gu_input_scale_h;  // [E]
  std::vector<float> dn_w_scale2_h;  // [E]
  std::vector<float> dn_input_scale_h;  // [E]

  // Size of one expert's swizzled gate/up SF block.
  size_t gu_sf_block() const { return SfBufferSize(2 * moe_is, hs); }
  // Size of one expert's swizzled down SF block.
  size_t dn_sf_block() const { return SfBufferSize(hs, moe_is); }

  // Pointer to the merged gate/up packed weights of expert `e`. The slice
  // holds 2*moe_is rows (gate then up), each hs/2 bytes, so the per-expert
  // stride is 2*moe_is * hs/2 = moe_is * hs bytes.
  uint8_t* gu_packed_expert(int e) const {
    return gu_packed + static_cast<size_t>(e) * moe_is * hs;
  }
  // Pointer to expert `e`'s swizzled gate/up SF block.
  uint8_t* gu_sf_expert(int e) const {
    return gu_sf + static_cast<size_t>(e) * gu_sf_block();
  }
  // Pointer to the down packed weights of expert `e`.
  uint8_t* dn_packed_expert(int e) const {
    return dn_packed + static_cast<size_t>(e) * hs * (moe_is / 2);
  }
  // Pointer to expert `e`'s swizzled down SF block.
  uint8_t* dn_sf_expert(int e) const {
    return dn_sf + static_cast<size_t>(e) * dn_sf_block();
  }

  // Total device bytes (for diagnostics / allocation accounting).
  size_t TotalBytes() const {
    size_t b = 0;
    b += static_cast<size_t>(2 * E * moe_is) * (hs / 2);
    b += static_cast<size_t>(E) * gu_sf_block();
    b += static_cast<size_t>(E * hs) * (moe_is / 2);
    b += static_cast<size_t>(E) * dn_sf_block();
    b += 4 * static_cast<size_t>(E) * sizeof(float);
    return b;
  }
};

// Load one layer's routed-expert NVFP4 weights into `out` (device memory).
//
// Reads the 12*E expert tensors (weight / weight_scale / weight_scale_2 /
// input_scale for gate / up / down) from `loader` under the checkpoint names
//   model.language_model.layers.{layer_id}.mlp.experts.{e}.{proj}.{suffix}
// and places them into the pre-allocated buffers. weight_scale tensors are
// swizzled on the host before the H2D copy.
//
// `E`, `hs`, `moe_is` come from the model config. Returns a Status; on
// success the caller owns all buffers in `out` (free with cudaFree).
Status LoadMoEWeights(const io::WeightLoader& loader, int layer_id, int E,
                      int hs, int moe_is, MoEWeightLayout* out,
                      cudaStream_t stream);

}  // namespace quant
}  // namespace q4t
