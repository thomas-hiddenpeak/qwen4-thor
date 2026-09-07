// Image processor for the Qwen3-VL vision tower.
//
// Decodes PNG/JPEG, smart-resizes (factor = patch_size * merge_size),
// applies BICUBIC resize (exact Pillow 12.3.0 fixed-point replication),
// normalizes to [-1, 1], and patchifies in block-major order to match
// the vision tower's expected pixel_values layout.
//
// Reference: transformers 5.16.1 Qwen2VLImageProcessorPil (PIL backend)
// + Pillow 12.3.0 src/libImaging/Resample.c (bicubic fixed-point).
//
// Pipeline (must match the Python reference tools/vision_processor_ref.py):
//   1. Decode PNG/JPEG -> RGB uint8 [H, W, 3]
//   2. smart_resize(H, W, factor=32, min_pixels, max_pixels) -> (rh, rw)
//   3. BICUBIC resize (Pillow fixed-point) -> uint8 [rh, rw, 3]
//   4. rescale: / 255.0 -> float32 [0, 1]
//   5. normalize: (x - mean) / std -> float32 (mean=std=0.5 -> [-1, 1])
//   6. patchify: block-major, per-patch [C=3, T=2, P=16, P=16]
//      (single frame repeated T times)
#pragma once

#include <cstddef>
#include <cstdint>
#include <string>
#include <vector>

namespace q4t {
namespace vision {

// Processor config (from preprocessor_config.json).
struct ProcessorConfig {
  int patch_size = 16;
  int temporal_patch_size = 2;
  int merge_size = 2;
  int min_pixels = 65536;
  int max_pixels = 16777216;
  float image_mean[3] = {0.5f, 0.5f, 0.5f};
  float image_std[3] = {0.5f, 0.5f, 0.5f};
};

// Processed image (ready for the vision tower).
struct ProcessedImage {
  std::vector<float> pixel_values;  // [L, C*T*P*P] float32, block-major
  int grid_t = 1;
  int grid_h = 0;
  int grid_w = 0;
  ProcessorConfig cfg;  // the config used

  int L() const { return grid_h * grid_w * grid_t; }
  int patch_dim() const {
    return 3 * cfg.temporal_patch_size * cfg.patch_size * cfg.patch_size;
  }
};

// Decode + resize + normalize + patchify.
// image_bytes: raw PNG/JPEG image data.
// num_bytes: length of image_bytes.
// cfg: processor config.
// out: receives the processed image.
// err: error message on failure.
// Returns false on failure.
bool ProcessImage(const uint8_t* image_bytes, size_t num_bytes,
                  const ProcessorConfig& cfg, ProcessedImage* out,
                  std::string* err);

}  // namespace vision
}  // namespace q4t
