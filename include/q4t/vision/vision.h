// Vision tower (Qwen3_VisionTransformer) for multimodal input.
//
// Consumes preprocessed pixel patches (layout [L, C*T*P*P] per patch, i.e.
// [C, T, P, P] within each patch; for a single image T=1 so [C, P, P]) and
// produces merged visual features [L/4, out_hidden] to be injected into the
// LLM in place of image tokens.
//
// Pipeline (matches reference/vllm .../qwen3_vl.py Qwen3_VisionTransformer
// and the numpy CPU reference tools/vision_reference.py):
//   1. patch_embed: Conv3d(in=3, out=1152, k=(2,16,16), s=(2,16,16))
//   2. + pos_embed: bilinear-interpolated 2D positional embedding
//   3. 27 blocks: x += attn(LN(x)); x += mlp(LN(x))
//        attn: QKV proj -> 16 heads (head_dim=72) -> 2D RoPE ->
//              bidirectional softmax attention -> out proj
//        mlp:  fc1 -> GELU(tanh) -> fc2
//   4. merger: LayerNorm(1152) -> 2x2 spatial merge -> fc1 -> GELU -> fc2
//
// All GEMMs reuse model::Bf16Gemm (checkpoint row-major [N,K] weights).
#pragma once

#include <cuda_runtime.h>

#include <cstdint>
#include <cstddef>
#include <string>
#include <vector>

namespace q4t {
namespace io {
class WeightLoader;
}  // namespace io
namespace vision {

// Vision tower config (from config.json vision_config).
struct VisionConfig {
  int depth = 27;             // number of transformer blocks
  int hidden_size = 1152;     // ViT hidden dim
  int num_heads = 16;         // attention heads
  int head_dim = 72;          // head_dim = hidden_size / num_heads
  int intermediate_size = 4304;  // MLP fc1 out
  int patch_size = 16;
  int spatial_merge_size = 2;
  int temporal_patch_size = 2;
  int in_channels = 3;
  int out_hidden_size = 2560;  // LLM hidden size (merger fc2 out)
  int num_position_embeddings = 2304;  // 48x48 pos grid
  float eps = 1e-6f;
};

// Per-image geometry (derived from the preprocessor).
struct ImageShape {
  int h = 0;  // grid height (number of patches vertically)
  int w = 0;  // grid width
  int t = 1;  // temporal frames (1 for a still image)
  int L() const { return h * w * t; }  // number of patches
};

// Vision tower weights (all device BF16, checkpoint row-major [N,K]).
struct VisionWeights {
  // patch_embed Conv3d: weight [out, in*C*T*P*P] = [1152, 96], bias [1152].
  uint16_t* d_patch_w = nullptr;  // [hidden, in*tp*P*P]
  uint16_t* d_patch_b = nullptr;  // [hidden]
  // pos_embed [num_position_embeddings, hidden] = [2304, 1152].
  uint16_t* d_pos_embed = nullptr;
  // Per-block (depth of them):
  std::vector<uint16_t*> d_ln1_w;  // [hidden]
  std::vector<uint16_t*> d_ln1_b;  // [hidden]
  std::vector<uint16_t*> d_qkv_w;  // [3*hidden, hidden]
  std::vector<uint16_t*> d_qkv_b;  // [3*hidden]
  std::vector<uint16_t*> d_attn_proj_w;  // [hidden, hidden]
  std::vector<uint16_t*> d_attn_proj_b;  // [hidden]
  std::vector<uint16_t*> d_ln2_w;        // [hidden]
  std::vector<uint16_t*> d_ln2_b;        // [hidden]
  std::vector<uint16_t*> d_fc1_w;        // [intermediate, hidden]
  std::vector<uint16_t*> d_fc1_b;        // [intermediate]
  std::vector<uint16_t*> d_fc2_w;        // [hidden, intermediate]
  std::vector<uint16_t*> d_fc2_b;        // [hidden]
  // merger:
  uint16_t* d_merger_ln_w = nullptr;  // [hidden]
  uint16_t* d_merger_ln_b = nullptr;  // [hidden]
  uint16_t* d_merger_fc1_w = nullptr;  // [4*hidden, 4*hidden]
  uint16_t* d_merger_fc1_b = nullptr;  // [4*hidden]
  uint16_t* d_merger_fc2_w = nullptr;  // [out_hidden, 4*hidden]
  uint16_t* d_merger_fc2_b = nullptr;  // [out_hidden]

  void Free();
};

// Vision tower: config + weights + precomputed per-image tables + workspace.
struct VisionTower {
  VisionConfig cfg;
  VisionWeights w;

  // Precomputed 2D RoPE cos/sin tables (device float), sized for the largest
  // grid seen. Each is [max_grid, 18]: h-half (cos_h/sin_h) and w-half
  // (cos_w/sin_w), neox pairing (i, i+18) for i in [0,18).
  float* d_cos = nullptr;  // cos_h
  float* d_sin = nullptr;  // sin_h
  float* d_cos_w = nullptr;  // cos_w
  float* d_sin_w = nullptr;  // sin_w
  int max_grid = 0;

  // Per-image bilinear pos-embed tables (device): for each patch p, the four
  // corner row indices (int) and weights (float) into d_pos_embed.
  int* d_pos_rows = nullptr;    // [L, 4]
  float* d_pos_weights = nullptr;  // [L, 4]
  // Per-patch 2D position IDs (block-major order): [L, 2] = (h_pos, w_pos).
  int* d_pos_ids = nullptr;

  // Device workspace (intermediate activations).
  uint16_t* d_ws = nullptr;
  size_t ws_bytes = 0;

  // Host-side per-image scratch (rot_pos_ids etc.), freed with the tower.
  std::vector<uint8_t> host_scratch;

  // Allocate workspace for the given image shapes. Returns false on OOM.
  // Idempotent: safe to call again (e.g. per request) — any prior workspace
  // and (if the grid grew) RoPE tables are freed and rebuilt.
  bool Allocate(const std::vector<ImageShape>& shapes,
                cudaStream_t stream);
  void Free();
};

// Load vision weights from an already-opened main-model WeightLoader (tensors
// under "model.visual."). Returns false (with err) on missing tensors or
// allocation failure.
bool LoadVision(const io::WeightLoader& loader, const VisionConfig& cfg,
                VisionTower* tower, std::string* err,
                cudaStream_t stream = 0);

// Run the vision tower forward for one or more images.
//
// pixel_values: device BF16 [total_L, in*tp*P*P] (patches concatenated in
//   image order, each patch laid out [C, T, P, P]).
// shapes: per-image geometry; sum of L() must equal total_L.
// out: device BF16 [total_L/merge^2, out_hidden] (merged features, in image
//   order). Caller allocates (see VisionOutputBytes).
//
// Returns false (with err) on failure.
bool VisionForward(const VisionTower& tower, const uint16_t* pixel_values,
                   const std::vector<ImageShape>& shapes, uint16_t* out,
                   std::string* err, cudaStream_t stream = 0);

// Required output bytes for the given shapes.
inline size_t VisionOutputBytes(const VisionConfig& cfg,
                                const std::vector<ImageShape>& shapes) {
  size_t merged = 0;
  const int m = cfg.spatial_merge_size;
  for (const auto& s : shapes) {
    merged += static_cast<size_t>(s.h / m) * (s.w / m) * s.t;
  }
  return merged * static_cast<size_t>(cfg.out_hidden_size) * sizeof(uint16_t);
}

}  // namespace vision
}  // namespace q4t
