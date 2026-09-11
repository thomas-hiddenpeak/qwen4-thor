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

// Processed video (ready for the vision tower). Same per-patch layout as
// ProcessedImage ([C, T, P, P], block-major spatial within each time group),
// but T = temporal_patch_size real frames per group (not a repeated frame).
struct ProcessedVideo {
  std::vector<float> pixel_values;  // [L, C*T*P*P] float32, block-major
  int grid_t = 0;
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

// Video processor (Qwen3-VL, transformers 5.16.1 Qwen3VLVideoProcessor).
//
// frames: list of raw PNG/JPEG frame buffers (each decoded independently).
// frame_bytes: lengths of each frame buffer.
// cfg: processor config. NOTE: min_pixels/max_pixels must be the VIDEO
//      budget (video_preprocessor_config.json: 4096 / 25165824), NOT the
//      image budget.
// out: receives the processed video (grid_t = ceil(num_frames / temporal)).
// err: error message on failure.
// Returns false on failure.
//
// Pipeline (must match transformers Qwen3VLVideoProcessor):
//   1. Decode each frame (stb) -> RGB uint8 [H, W, 3] (all frames same size)
//   2. video smart_resize(H, W, num_frames, factor=32, temporal_factor=2,
//      min_pixels, max_pixels) -> (rh, rw)  [t*h*w budget]
//   3. Per-frame BICUBIC resize (Pillow fixed-point; the reference uses
//      torchvision BICUBIC+antialias which differs by <= 1/255 per pixel)
//   4. Odd-frame pad: repeat the LAST frame until num_frames is even
//   5. rescale /255 + normalize (x-0.5)/0.5 (per frame)
//   6. patchify: per-patch [C, T, P, P] with T real frames, block-major
//      spatial within each time group (identical layout to ProcessImage).
bool ProcessVideo(const std::vector<const uint8_t*>& frames,
                  const std::vector<size_t>& frame_bytes,
                  const ProcessorConfig& cfg, ProcessedVideo* out,
                  std::string* err);

}  // namespace vision
}  // namespace q4t
