// Existing ChatServer responsibilities; shared state stays in ChatServer.
#include "chat_server_internal.h"

#include <algorithm>
#include <cstdio>
#include <cstring>
#include <string>
#include <utility>
#include <vector>
#include <cuda_bf16.h>
#include <cuda_runtime.h>
#include "q4t/server/chat_template.h"
#include "q4t/vision/processor.h"
#include "q4t/vision/vision.h"

namespace q4t::server {
bool ChatServer::RunVisionPipeline(const std::vector<VisionItem>& items,
                                   uint16_t** out_feats, int* out_num_tokens,
                                   std::vector<int>* out_counts,
                                   std::vector<std::array<int, 3>>* out_grids,
                                   std::string* err) {
  *out_feats = nullptr;
  *out_num_tokens = 0;
  if (out_grids) out_grids->clear();
  if (vision_tower_ == nullptr) {
    if (err) *err = "vision tower not loaded (text-only model)";
    return false;
  }
  if (items.empty()) return true;  // nothing to do

  const vision::VisionConfig& cfg = vision_tower_->cfg;
  const int m = cfg.spatial_merge_size;

  // 1. Process each item (CPU: decode + resize + normalize + patchify).
  //    Images use the image budget (proc_cfg_); videos use the video budget
  //    (video_proc_cfg_). Both produce the same per-patch layout [C,T,P,P]
  //    (96 floats) so they can share one VisionForward batch.
  std::vector<std::vector<float>> pixel_sets;  // per-item float32 patches
  pixel_sets.reserve(items.size());
  std::vector<vision::ImageShape> shapes;
  shapes.reserve(items.size());
  int total_L = 0;
  for (const auto& item : items) {
    std::vector<float> pix;
    vision::ImageShape sh;
    std::string perr;
    if (item.kind == VisionItem::kImage) {
      vision::ProcessedImage pi;
      if (!vision::ProcessImage(
              reinterpret_cast<const uint8_t*>(item.frames[0].data()),
              item.frames[0].size(), proc_cfg_, &pi, &perr)) {
        if (err) *err = "image process failed: " + perr;
        return false;
      }
      pix = std::move(pi.pixel_values);
      sh.h = pi.grid_h;
      sh.w = pi.grid_w;
      sh.t = pi.grid_t;
    } else {
      std::vector<const uint8_t*> fptrs;
      std::vector<size_t> flens;
      fptrs.reserve(item.frames.size());
      flens.reserve(item.frames.size());
      for (const auto& f : item.frames) {
        fptrs.push_back(reinterpret_cast<const uint8_t*>(f.data()));
        flens.push_back(f.size());
      }
      vision::ProcessedVideo pv;
      if (!vision::ProcessVideo(fptrs, flens, video_proc_cfg_, &pv, &perr)) {
        if (err) *err = "video process failed: " + perr;
        return false;
      }
      pix = std::move(pv.pixel_values);
      sh.h = pv.grid_h;
      sh.w = pv.grid_w;
      sh.t = pv.grid_t;
    }
    shapes.push_back(sh);
    total_L += sh.L();
    pixel_sets.push_back(std::move(pix));
    // Patch-unit grid (t, h, w) for the 3D MRoPE table (same order as the
    // feature rows / placeholders).
    if (out_grids) out_grids->push_back({sh.t, sh.h, sh.w});
  }

  // 2. Build the device pixel buffer (float32 -> BF16, concatenated).
  const int patch_dim = cfg.in_channels * cfg.temporal_patch_size *
                        cfg.patch_size * cfg.patch_size;  // 96
  std::vector<uint16_t> pixels_bf16(static_cast<size_t>(total_L) * patch_dim);
  {
    size_t off = 0;
    for (const auto& pix : pixel_sets) {
      const size_t n = pix.size();
      for (size_t i = 0; i < n; ++i) {
        const __nv_bfloat16 b = __float2bfloat16_rn(pix[i]);
        pixels_bf16[off + i] = *reinterpret_cast<const uint16_t*>(&b);
      }
      off += n;
    }
  }
  uint16_t* d_pixels = nullptr;
  if (cudaMalloc(reinterpret_cast<void**>(&d_pixels),
                 pixels_bf16.size() * sizeof(uint16_t)) != cudaSuccess) {
    if (err) *err = "cudaMalloc pixels failed";
    return false;
  }
  if (cudaMemcpy(d_pixels, pixels_bf16.data(),
                 pixels_bf16.size() * sizeof(uint16_t),
                 cudaMemcpyHostToDevice) != cudaSuccess) {
    cudaFree(d_pixels);
    if (err) *err = "H2D pixels failed";
    return false;
  }

  // 3. Allocate the tower workspace + run the forward.
  if (!vision_tower_->Allocate(shapes, nullptr)) {
    cudaFree(d_pixels);
    if (err) *err = "vision Allocate failed";
    return false;
  }
  const size_t out_bytes = vision::VisionOutputBytes(cfg, shapes);
  uint16_t* d_out = nullptr;
  if (cudaMalloc(reinterpret_cast<void**>(&d_out), out_bytes) != cudaSuccess) {
    cudaFree(d_pixels);
    if (err) *err = "cudaMalloc vision output failed";
    return false;
  }
  std::string ferr;
  if (!vision::VisionForward(*vision_tower_, d_pixels, shapes, d_out, &ferr,
                             nullptr)) {
    cudaFree(d_pixels);
    cudaFree(d_out);
    if (err) *err = "VisionForward failed: " + ferr;
    return false;
  }
  cudaFree(d_pixels);
  cudaDeviceSynchronize();

  // 4. Per-item merged-token counts (the <|image_pad|> / <|video_pad|>
  //    expansion factor). One count per VisionItem, in item (prompt position)
  //    order.
  out_counts->clear();
  int total_tokens = 0;
  for (const auto& sh : shapes) {
    const int c = (sh.h / m) * (sh.w / m) * sh.t;
    out_counts->push_back(c);
    total_tokens += c;
  }
  *out_feats = d_out;
  *out_num_tokens = total_tokens;
  return true;
}

}  // namespace q4t::server
