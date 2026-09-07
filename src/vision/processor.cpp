// Image processor for the Qwen3-VL vision tower.
//
// Exact replication of transformers 5.16.1 Qwen2VLImageProcessorPil (PIL
// backend) + Pillow 12.3.0 BICUBIC fixed-point resize.
//
// Pipeline:
//   1. Decode PNG/JPEG (stb_image) -> RGB uint8 [H, W, 3]
//   2. smart_resize(H, W, factor=32, min_pixels, max_pixels) -> (rh, rw)
//   3. BICUBIC resize (Pillow fixed-point, PRECISION_BITS=22) -> uint8
//   4. rescale: float32(double(px) * double(1.0/255.0))
//   5. normalize: (f32 - 0.5f) / 0.5f  (mean=std=0.5)
//   6. patchify: block-major, per-patch [C=3, T=2, P=16, P=16]
#include "q4t/vision/processor.h"

#include <cmath>
#include <cstdint>
#include <cstring>
#include <string>
#include <vector>

// stb_image for PNG/JPEG decoding.
#define STB_IMAGE_IMPLEMENTATION
#include "stb/stb_image.h"

namespace q4t {
namespace vision {

namespace {

// ---------------------------------------------------------------------------
// smart_resize (exact copy of transformers 5.16.1).
// ---------------------------------------------------------------------------
bool SmartResize(int height, int width, int factor, int min_pixels,
                 int max_pixels, int* out_h, int* out_w) {
  if (height <= 0 || width <= 0) return false;
  if (static_cast<double>(std::max(height, width)) /
          std::min(height, width) >
      200.0) {
    return false;  // aspect ratio too large
  }
  int h_bar = static_cast<int>(std::round(static_cast<double>(height) / factor)) *
              factor;
  int w_bar = static_cast<int>(std::round(static_cast<double>(width) / factor)) *
              factor;
  if (static_cast<int64_t>(h_bar) * w_bar > max_pixels) {
    double beta = std::sqrt(
        static_cast<double>(height * width) / static_cast<double>(max_pixels));
    h_bar = std::max(factor,
                     static_cast<int>(std::floor(
                         static_cast<double>(height) / beta / factor)) *
                         factor);
    w_bar = std::max(factor,
                     static_cast<int>(std::floor(
                         static_cast<double>(width) / beta / factor)) *
                         factor);
  } else if (static_cast<int64_t>(h_bar) * w_bar < min_pixels) {
    double beta = std::sqrt(
        static_cast<double>(min_pixels) / static_cast<double>(height * width));
    h_bar = static_cast<int>(std::ceil(
        static_cast<double>(height) * beta / factor)) *
            factor;
    w_bar = static_cast<int>(std::ceil(
        static_cast<double>(width) * beta / factor)) *
            factor;
  }
  *out_h = h_bar;
  *out_w = w_bar;
  return true;
}

// ---------------------------------------------------------------------------
// Pillow 12.3.0 BICUBIC fixed-point resize (exact replication).
// Reference: src/libImaging/Resample.c
// ---------------------------------------------------------------------------
constexpr int kPrecisionBits = 22;  // 32 - 8 - 2

// bicubic_filter (a = -0.5).
inline double BicubicFilter(double x) {
  if (x < 0.0) x = -x;
  if (x < 1.0) {
    return ((-0.5 + 2.0) * x - (-0.5 + 3.0)) * x * x + 1.0;
  }
  if (x < 2.0) {
    return (((x - 5.0) * x + 8.0) * x - 4.0) * (-0.5);
  }
  return 0.0;
}

// clip8: clamp(ss >> 22, 0, 255).
inline uint8_t Clip8(int32_t in) {
  int32_t v = in >> kPrecisionBits;
  if (v < 0) return 0;
  if (v > 255) return 255;
  return static_cast<uint8_t>(v);
}

struct ResampleCoeffs {
  int ksize = 0;
  std::vector<int> bounds;  // [outSize * 2] = (xmin, xmax) per output
  std::vector<int32_t> kk;  // [outSize * ksize] fixed-point weights
};

// precompute_coeffs + normalize_coeffs_8bpc (combined).
ResampleCoeffs PrecomputeCoeffs(int in_size, double in0, double in1,
                                int out_size) {
  ResampleCoeffs r;
  const double support = 2.0;  // BICUBIC support
  double scale = (in1 - in0) / out_size;
  double filterscale = scale;
  if (filterscale < 1.0) filterscale = 1.0;
  double sup = support * filterscale;
  int ksize = static_cast<int>(std::ceil(sup)) * 2 + 1;
  r.ksize = ksize;
  r.bounds.resize(out_size * 2);
  r.kk.resize(out_size * ksize);

  double inv_filterscale = 1.0 / filterscale;
  for (int xx = 0; xx < out_size; ++xx) {
    double center = in0 + (xx + 0.5) * scale;
    double ww = 0.0;
    int xmin = static_cast<int>(center - sup + 0.5);
    if (xmin < 0) xmin = 0;
    int xmax = static_cast<int>(center + sup + 0.5);
    if (xmax > in_size) xmax = in_size;
    int count = xmax - xmin;
    xmax = count;  // store count as "xmax" (Pillow convention)
    for (int x = 0; x < count; ++x) {
      double w = BicubicFilter((x + xmin - center + 0.5) * inv_filterscale);
      r.kk[xx * ksize + x] = 0;  // will be set after normalization
      ww += w;
      // Store raw weight temporarily (recompute after normalization).
      r.kk[xx * ksize + x] =
          static_cast<int32_t>(std::round(w * (1 << kPrecisionBits)));
    }
    // Normalize by sum.
    if (ww != 0.0) {
      for (int x = 0; x < count; ++x) {
        // Recompute: w / ww, then to fixed-point.
        double w = BicubicFilter((x + xmin - center + 0.5) * inv_filterscale);
        w /= ww;
        // normalize_coeffs_8bpc: round to int32.
        int32_t kv;
        if (w < 0) {
          kv = static_cast<int32_t>(-0.5 + w * (1 << kPrecisionBits));
        } else {
          kv = static_cast<int32_t>(0.5 + w * (1 << kPrecisionBits));
        }
        r.kk[xx * ksize + x] = kv;
      }
    }
    // Zero remaining.
    for (int x = count; x < ksize; ++x) {
      r.kk[xx * ksize + x] = 0;
    }
    r.bounds[xx * 2 + 0] = xmin;
    r.bounds[xx * 2 + 1] = xmax;
  }
  return r;
}

// One-pass resize (horizontal or vertical) on a single channel of a
// channel-last [h, w, 3] image. `in`/`out` point at the channel plane
// (channel `c` is at in[y*w+x]*3+c). in: [in_h, in_w], out: [out_h, out_w].
void ResamplePass(const uint8_t* in, int in_h, int in_w, int in_stride,
                  uint8_t* out, int out_h, int out_w, int out_stride,
                  const ResampleCoeffs& coeffs, bool horizontal) {
  if (horizontal) {
    // For each row, for each output x: weighted sum over input x.
    for (int y = 0; y < in_h; ++y) {
      const uint8_t* line_in = in + y * in_stride;
      uint8_t* line_out = out + y * out_stride;
      for (int xx = 0; xx < out_w; ++xx) {
        int xmin = coeffs.bounds[xx * 2 + 0];
        int xmax = coeffs.bounds[xx * 2 + 1];
        const int32_t* k = &coeffs.kk[xx * coeffs.ksize];
        int32_t ss = 1 << (kPrecisionBits - 1);
        for (int x = 0; x < xmax; ++x) {
          ss += static_cast<int32_t>(line_in[(x + xmin) * 3]) * k[x];
        }
        line_out[xx * 3] = Clip8(ss);
      }
    }
  } else {
    // For each column, for each output y: weighted sum over input y.
    for (int x = 0; x < in_w; ++x) {
      for (int yy = 0; yy < out_h; ++yy) {
        int ymin = coeffs.bounds[yy * 2 + 0];
        int ymax = coeffs.bounds[yy * 2 + 1];
        const int32_t* k = &coeffs.kk[yy * coeffs.ksize];
        int32_t ss = 1 << (kPrecisionBits - 1);
        for (int y = 0; y < ymax; ++y) {
          ss += static_cast<int32_t>(in[(y + ymin) * in_stride + x * 3]) * k[y];
        }
        out[yy * out_stride + x * 3] = Clip8(ss);
      }
    }
  }
}

// Two-pass BICUBIC resize (Pillow: horizontal first, then vertical).
// in: [h, w, 3] uint8 (channel-last), out: [out_h, out_w, 3] uint8.
// Operates in-place per channel via the stride parameter (no channel
// splitting): channel c of pixel (y, x) is at buffer[y*stride + x*3 + c].
void BicubicResize(const uint8_t* in, int h, int w, uint8_t* out, int out_h,
                   int out_w) {
  const bool need_h = (w != out_w);
  const bool need_v = (h != out_h);

  // Precompute vertical coeffs (always needed unless identity).
  ResampleCoeffs vert = PrecomputeCoeffs(h, 0, h, out_h);

  if (need_h) {
    // Horizontal pass: [h, w, 3] -> [h, out_w, 3].
    ResampleCoeffs horiz = PrecomputeCoeffs(w, 0, w, out_w);
    std::vector<uint8_t> tmp(static_cast<size_t>(h) * out_w * 3);
    for (int c = 0; c < 3; ++c) {
      ResamplePass(in + c, h, w, w * 3, tmp.data() + c, h, out_w, out_w * 3,
                   horiz, true);
    }
    // Vertical pass: [h, out_w, 3] -> [out_h, out_w, 3].
    for (int c = 0; c < 3; ++c) {
      ResamplePass(tmp.data() + c, h, out_w, out_w * 3, out + c, out_h, out_w,
                   out_w * 3, vert, false);
    }
  } else if (need_v) {
    // Vertical only: [h, w, 3] -> [out_h, w, 3] (w == out_w).
    for (int c = 0; c < 3; ++c) {
      ResamplePass(in + c, h, w, w * 3, out + c, out_h, out_w, out_w * 3, vert,
                   false);
    }
  }
  // else: identity (h == out_h && w == out_w), caller copies.
}

}  // namespace

// ---------------------------------------------------------------------------
// ProcessImage: decode + resize + normalize + patchify.
// ---------------------------------------------------------------------------
bool ProcessImage(const uint8_t* image_bytes, size_t num_bytes,
                  const ProcessorConfig& cfg, ProcessedImage* out,
                  std::string* err) {
  if (!image_bytes || num_bytes == 0) {
    if (err) *err = "empty image data";
    return false;
  }
  if (!out) {
    if (err) *err = "null output";
    return false;
  }

  // 1. Decode PNG/JPEG -> RGB uint8.
  int w = 0, h = 0, comp = 0;
  uint8_t* pixels =
      stbi_load_from_memory(image_bytes, static_cast<int>(num_bytes), &w, &h,
                            &comp, 3);  // force 3 channels (RGB)
  if (!pixels) {
    if (err) *err = "failed to decode image (not PNG/JPEG?)";
    return false;
  }
  if (w <= 0 || h <= 0) {
    stbi_image_free(pixels);
    if (err) *err = "invalid image dimensions";
    return false;
  }

  // 2. smart_resize.
  const int factor = cfg.patch_size * cfg.merge_size;  // 16*2 = 32
  int rh = 0, rw = 0;
  if (!SmartResize(h, w, factor, cfg.min_pixels, cfg.max_pixels, &rh, &rw)) {
    stbi_image_free(pixels);
    if (err) *err = "smart_resize failed (aspect ratio or size)";
    return false;
  }

  // 3. BICUBIC resize (Pillow fixed-point).
  std::vector<uint8_t> resized;
  if (rh != h || rw != w) {
    resized.resize(static_cast<size_t>(rh) * rw * 3);
    BicubicResize(pixels, h, w, resized.data(), rh, rw);
  } else {
    resized.assign(pixels, pixels + static_cast<size_t>(h) * w * 3);
  }
  stbi_image_free(pixels);

  // 4-5. rescale + normalize (per pixel, per channel, exact float32).
  // rescale: float32(double(px) * double(1.0/255.0))
  // normalize: (f32 - mean[c]) / std[c]
  // Since 1/255.0 in double is the exact Python literal, this matches
  // transformers' `float64(px) * scale` then `astype(float32)`.
  const double kInv255 = 1.0 / 255.0;
  std::vector<float> norm(static_cast<size_t>(rh) * rw * 3);
  for (size_t i = 0; i < norm.size(); ++i) {
    const int c = static_cast<int>(i % 3);
    float f = static_cast<float>(static_cast<double>(resized[i]) * kInv255);
    norm[i] = (f - cfg.image_mean[c]) / cfg.image_std[c];
  }

  // 6. Patchify: block-major, per-patch [C=3, T=2, P=16, P=16].
  const int P = cfg.patch_size;
  const int T = cfg.temporal_patch_size;
  const int M = cfg.merge_size;
  const int grid_h = rh / P;
  const int grid_w = rw / P;
  const int patch_dim = 3 * T * P * P;
  const int L = grid_h * grid_w;

  out->cfg = cfg;
  out->grid_t = 1;
  out->grid_h = grid_h;
  out->grid_w = grid_w;
  out->pixel_values.resize(static_cast<size_t>(L) * patch_dim);

  // norm is [rh, rw, 3] (channel-last). For patch (i, j):
  //   tile[p, q, c] = norm[(i*P+p) * rw + (j*P+q)] * 3 + c
  // Per-patch output layout: [C, T, P, P] = for c in 0..2, for t in 0..T-1,
  //   for p in 0..P-1, for q in 0..P-1: tile[p, q, c]
  // (single frame repeated T times, so t=0 and t=1 are identical)
  for (int hb = 0; hb < grid_h / M; ++hb) {
    for (int wb = 0; wb < grid_w / M; ++wb) {
      for (int mb = 0; mb < M; ++mb) {
        for (int mj = 0; mj < M; ++mj) {
          const int i = hb * M + mb;  // patch row
          const int j = wb * M + mj;  // patch col
          const int p_idx = (hb * (grid_w / M) + wb) * (M * M) + mb * M + mj;
          float* dst = out->pixel_values.data() +
                       static_cast<size_t>(p_idx) * patch_dim;
          // Fill [C, T, P, P].
          for (int c = 0; c < 3; ++c) {
            for (int t = 0; t < T; ++t) {
              float* plane = dst + (c * T + t) * P * P;
              for (int p = 0; p < P; ++p) {
                for (int q = 0; q < P; ++q) {
                  // norm is [rh, rw, 3]: index = (row_idx * rw + col_idx) * 3 + c
                  plane[p * P + q] =
                      norm[(static_cast<size_t>(i * P + p) * rw +
                            (j * P + q)) * 3 + c];
                }
              }
            }
          }
        }
      }
    }
  }

  return true;
}

}  // namespace vision
}  // namespace q4t
