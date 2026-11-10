// Image processor for the Qwen3-VL vision tower.
//
// Resize/normalize/patch layout follows transformers 5.16.1 PIL backend.
// PNG RGB decode is exact; stb JPEG decoding is not bit-identical to Pillow.
// BICUBIC uses Pillow 12.3.0 fixed-point coefficients.
//
// Pipeline:
//   1. Decode PNG/JPEG (stb_image) -> RGB uint8 [H, W, 3]
//   2. smart_resize(H, W, factor=32, min_pixels, max_pixels) -> (rh, rw)
//   3. BICUBIC resize (Pillow fixed-point, PRECISION_BITS=22) -> uint8
//   4. rescale: float32(double(px) * double(1.0/255.0))
//   5. normalize: (f32 - 0.5f) / 0.5f  (mean=std=0.5)
//   6. patchify: block-major, per-patch [C=3, T=2, P=16, P=16]
#include "q4t/vision/processor.h"
#include "q4t/runtime/execution_progress.h"
#include "q4t/vision/media_plan.h"
#include "q4t/vision/timing.h"

#include <algorithm>
#include <array>
#include <cmath>
#include <cstdint>
#include <cstdlib>
#include <cstring>
#include <memory>
#include <limits>
#include <new>
#include <string>
#include <vector>

namespace {
thread_local bool decode_allocation_failed = false;
struct DecodeBudget {
  size_t maximum;
  size_t owned = 0;
  size_t peak = 0;
  bool denied = false;
};
thread_local DecodeBudget* decode_budget = nullptr;
struct DecodeScope {
  DecodeBudget budget;
  DecodeBudget* previous;
  q4t::vision::ProcessorResult* result;
  explicit DecodeScope(const q4t::vision::ProcessorOptions& options)
      : budget{options.decoder_owned_bytes},
        previous(decode_budget),
        result(options.result) {
    decode_budget = &budget;
    decode_allocation_failed = false;
  }
  ~DecodeScope() {
    if (result)
      result->decoder_peak_bytes =
          std::max(result->decoder_peak_bytes, budget.peak);
    decode_budget = previous;
  }
};
struct alignas(std::max_align_t) DecodeHeader {
  size_t charged;
  DecodeBudget* owner;
};

bool DecodeCharge(size_t bytes, size_t old, DecodeBudget* budget,
                  size_t* charged) noexcept {
  if (bytes > std::numeric_limits<size_t>::max() - sizeof(DecodeHeader)) {
    if (budget) budget->denied = true;
    return false;
  }
  *charged = bytes + sizeof(DecodeHeader);
  if (budget && (budget->owned < old || *charged > budget->maximum ||
                 budget->owned - old > budget->maximum - *charged)) {
    budget->denied = true;
    return false;
  }
  return true;
}

void* DecodeAllocate(size_t bytes) noexcept {
  size_t charged = 0;
  if (!DecodeCharge(bytes, 0, decode_budget, &charged)) return nullptr;
  auto* header = static_cast<DecodeHeader*>(std::malloc(charged));
  if (!header) {
    decode_allocation_failed = true;
    return nullptr;
  }
  *header = {charged, decode_budget};
  if (decode_budget) {
    decode_budget->owned += charged;
    decode_budget->peak = std::max(decode_budget->peak, decode_budget->owned);
  }
  return header + 1;
}
void DecodeFree(void* pointer) noexcept {
  if (!pointer) return;
  auto* header = static_cast<DecodeHeader*>(pointer) - 1;
  if (header->owner) header->owner->owned -= header->charged;
  std::free(header);
}
void* DecodeReallocate(void* pointer, size_t bytes) noexcept {
  if (!pointer) return DecodeAllocate(bytes);
  if (!bytes) {
    DecodeFree(pointer);
    return nullptr;
  }
  auto* old_header = static_cast<DecodeHeader*>(pointer) - 1;
  const size_t old = old_header->charged;
  auto* owner = old_header->owner;
  size_t charged = 0;
  if (!DecodeCharge(bytes, old, owner, &charged)) return nullptr;
  auto* header = static_cast<DecodeHeader*>(std::realloc(old_header, charged));
  if (!header) {
    decode_allocation_failed = true;
    return nullptr;
  }
  *header = {charged, owner};
  if (owner) {
    owner->owned = owner->owned - old + charged;
    owner->peak = std::max(owner->peak, owner->owned);
  }
  return header + 1;
}
}  // namespace

// Let the C decoder unwind its own partial allocations before translating
// capacity failure to bad_alloc. Throwing from STBI_MALLOC would leak them.
#define STBI_MALLOC(bytes) DecodeAllocate(bytes)
#define STBI_REALLOC(pointer, bytes) DecodeReallocate(pointer, bytes)
#define STBI_FREE(pointer) DecodeFree(pointer)
#define STBI_ONLY_PNG
#define STBI_ONLY_JPEG
#define STB_IMAGE_IMPLEMENTATION
#include "stb/stb_image.h"

namespace q4t {
namespace vision {

namespace {

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

bool ProcessorFail(const ProcessorOptions& options, std::string* error,
                   const char* code) {
  if (options.result) options.result->code = code;
  if (error) *error = code;
  return false;
}

bool ProcessorContinue(const ProcessorOptions& options, std::string* error) {
  if (options.cancelled && options.cancelled(options.cancel_context))
    return ProcessorFail(options, error, "request_cancelled");
  return true;
}

bool PrepareItem(const MediaPlanInput& input, const ProcessorConfig& config,
                 const ProcessorOptions& options, MediaItemPlan* item,
                 std::string* error) {
  if (!options.decoder_owned_bytes)
    return ProcessorFail(options, error, "media_config_invalid");
  if (!ProcessorContinue(options, error)) return false;
  MediaPlan plan;
  const auto checked = PlanMediaSources({&input, 1}, config, config, {}, &plan);
  if (!checked) {
    if (options.result) options.result->frame = checked.frame;
    return ProcessorFail(options, error, MediaPlanErrorCode(checked.error));
  }
  *item = plan.items[0];
  if (const auto* admitted = options.admission) {
    const auto& a = admitted->geometry;
    const auto& b = item->geometry;
    if (admitted->kind != item->kind || admitted->detail != item->detail ||
        admitted->frames != item->frames ||
        admitted->source_width != item->source_width ||
        admitted->source_height != item->source_height || a.width != b.width ||
        a.height != b.height || a.grid_t != b.grid_t || a.grid_h != b.grid_h ||
        a.grid_w != b.grid_w || a.pixel_elements != b.pixel_elements ||
        a.patches != b.patches)
      return ProcessorFail(options, error, "media_plan_mismatch");
  }
  return ProcessorContinue(options, error);
}

template <class Output>
struct ProcessorOutput {
  Output* out;
  bool success = false;
  ~ProcessorOutput() {
    if (!success) *out = {};
  }
};

}  // namespace

// ---------------------------------------------------------------------------
// ProcessImage: decode + resize + normalize + patchify.
// ---------------------------------------------------------------------------
bool ProcessImage(const uint8_t* image_bytes, size_t num_bytes,
                  const ProcessorConfig& cfg, ProcessedImage* out,
                  std::string* err, const ProcessorOptions& options) {
  TimingScope prepare_timing(options.timings, TimingStage::kPrepare);
  if (options.result) *options.result = {};
  if (!out) return ProcessorFail(options, err, "media_config_invalid");
  *out = {};
  ProcessorOutput guard{out};
  if (!image_bytes || !num_bytes)
    return ProcessorFail(options, err, "media_encoded_limit");
  const MediaBytes bytes{image_bytes, num_bytes};
  const MediaPlanInput input{
      MediaKind::kImage, {&bytes, 1}, options.image_detail};
  MediaItemPlan item;
  if (!PrepareItem(input, cfg, options, &item, err)) return false;
  prepare_timing.Stop();
  DecodeScope decode(options);

  // 1. Decode PNG/JPEG -> RGB uint8.
  int w = 0, h = 0, comp = 0;
  decode_allocation_failed = false;
  // PrepareItem has already restricted sources to PNG/JPEG.
  runtime::ExecutionActivity decode_progress(
      runtime::ExecutionActivityKind::kMediaDecode);
  TimingScope decode_timing(options.timings, image_bytes[0] == 0xff
                                                ? TimingStage::kDecodeJpeg
                                                : TimingStage::kDecodePng);
  std::unique_ptr<uint8_t, decltype(&stbi_image_free)> decoded(
      stbi_load_from_memory(image_bytes, static_cast<int>(num_bytes), &w, &h,
                            &comp, 3),
      stbi_image_free);
  decode_timing.Stop();
  if (decode_allocation_failed) throw std::bad_alloc();
  if (decode.budget.denied)
    return ProcessorFail(options, err, "media_decode_limit");
  auto* pixels = decoded.get();
  if (!pixels) return ProcessorFail(options, err, "media_decode_invalid");
  if (w != static_cast<int>(item.source_width) ||
      h != static_cast<int>(item.source_height))
    return ProcessorFail(options, err, "media_decoded_dimensions");
  decode_progress.Finish();
  if (!ProcessorContinue(options, err)) return false;
  const int rh = item.geometry.height, rw = item.geometry.width;

  // 3. BICUBIC resize (Pillow fixed-point).
  runtime::ExecutionActivity resize_progress(
      runtime::ExecutionActivityKind::kMediaResize);
  TimingScope resize_timing(options.timings, TimingStage::kResize);
  std::vector<uint8_t> resized;
  if (rh != h || rw != w) {
    resized.resize(static_cast<size_t>(rh) * rw * 3);
    if (item.detail == ImageDetail::kOriginal) {
      // Preserve every original pixel, repeating only the right/bottom edge
      // to fill the model's spatial merge. Admission already charged padding.
      for (int y = 0; y < rh; ++y) {
        if (!(y % 128) && !ProcessorContinue(options, err)) return false;
        const auto* row =
            pixels + static_cast<size_t>(std::min(y, h - 1)) * w * 3;
        auto* output = resized.data() + static_cast<size_t>(y) * rw * 3;
        std::memcpy(output, row, static_cast<size_t>(w) * 3);
        for (int x = w; x < rw; ++x)
          std::memcpy(output + x * 3, row + (w - 1) * 3, 3);
      }
    } else {
      BicubicResize(pixels, h, w, resized.data(), rh, rw);
    }
  } else {
    resized.assign(pixels, pixels + static_cast<size_t>(h) * w * 3);
  }
  decoded.reset();
  resize_timing.Stop();
  resize_progress.Finish();
  if (!ProcessorContinue(options, err)) return false;

  // 4-5. rescale + normalize (per pixel, per channel, exact float32).
  // rescale: float32(double(px) * double(1.0/255.0))
  // normalize: (f32 - mean[c]) / std[c]
  // Since 1/255.0 in double is the exact Python literal, this matches
  // transformers' `float64(px) * scale` then `astype(float32)`.
  runtime::ExecutionActivity normalize_progress(
      runtime::ExecutionActivityKind::kMediaNormalize);
  TimingScope normalize_timing(options.timings, TimingStage::kNormalize);
  const double kInv255 = 1.0 / 255.0;
  std::vector<float> norm(static_cast<size_t>(rh) * rw * 3);
  for (size_t i = 0; i < norm.size(); ++i) {
    const int c = static_cast<int>(i % 3);
    float f = static_cast<float>(static_cast<double>(resized[i]) * kInv255);
    norm[i] = (f - cfg.image_mean[c]) / cfg.image_std[c];
  }
  normalize_timing.Stop();
  normalize_progress.Finish();

  // 6. Patchify: block-major, per-patch [C=3, T=2, P=16, P=16].
  runtime::ExecutionActivity patchify_progress(
      runtime::ExecutionActivityKind::kMediaPatchify);
  TimingScope patchify_timing(options.timings, TimingStage::kPatchify);
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

  patchify_timing.Stop();
  patchify_progress.Finish();
  guard.success = ProcessorContinue(options, err);
  return guard.success;
}

// ---------------------------------------------------------------------------
// ProcessVideo: decode frames + video smart_resize + per-frame BICUBIC +
// odd-frame pad + normalize + temporal patchify.
// ---------------------------------------------------------------------------
bool ProcessVideo(const std::vector<const uint8_t*>& frames,
                  const std::vector<size_t>& frame_bytes,
                  const ProcessorConfig& cfg, ProcessedVideo* out,
                  std::string* err, const ProcessorOptions& options) {
  TimingScope prepare_timing(options.timings, TimingStage::kPrepare);
  if (options.result) *options.result = {};
  if (!out) return ProcessorFail(options, err, "media_config_invalid");
  *out = {};
  ProcessorOutput guard{out};
  if (frames.size() < 2 || frames.size() > kMediaPlanFrames ||
      frame_bytes.size() != frames.size())
    return ProcessorFail(options, err, "media_frame_limit");
  std::array<MediaBytes, kMediaPlanFrames> bytes;
  for (size_t f = 0; f < frames.size(); ++f) {
    if (!frames[f] || !frame_bytes[f]) {
      if (options.result) options.result->frame = f;
      return ProcessorFail(options, err, "media_encoded_limit");
    }
    bytes[f] = {frames[f], frame_bytes[f]};
  }
  const MediaPlanInput input{MediaKind::kVideo, {bytes.data(), frames.size()}};
  MediaItemPlan item;
  if (!PrepareItem(input, cfg, options, &item, err)) return false;
  prepare_timing.Stop();
  const int F = static_cast<int>(frames.size());

  // 1. Decode every frame -> RGB uint8 [H, W, 3]. All frames share H, W.
  std::vector<std::vector<uint8_t>> decoded(F);
  const int H = static_cast<int>(item.source_height);
  const int W = static_cast<int>(item.source_width);
  for (int f = 0; f < F; ++f) {
    if (options.result) options.result->frame = f;
    if (!ProcessorContinue(options, err)) return false;
    DecodeScope decode(options);
    runtime::ExecutionActivity decode_progress(
        runtime::ExecutionActivityKind::kMediaDecode);
    TimingScope decode_timing(options.timings, frames[f][0] == 0xff
                                                  ? TimingStage::kDecodeJpeg
                                                  : TimingStage::kDecodePng);
    int w = 0, h = 0, comp = 0;
    decode_allocation_failed = false;
    std::unique_ptr<uint8_t, decltype(&stbi_image_free)> source(
        stbi_load_from_memory(frames[f], static_cast<int>(frame_bytes[f]), &w,
                              &h, &comp, 3),
        stbi_image_free);
    if (decode_allocation_failed) throw std::bad_alloc();
    if (decode.budget.denied)
      return ProcessorFail(options, err, "media_decode_limit");
    auto* px = source.get();
    if (!px) return ProcessorFail(options, err, "media_decode_invalid");
    if (h != H || w != W)
      return ProcessorFail(options, err, "media_decoded_dimensions");
    if (!ProcessorContinue(options, err)) return false;
    decoded[f].assign(px, px + static_cast<size_t>(h) * w * 3);
    decode_progress.Finish();
  }

  const int rh = item.geometry.height, rw = item.geometry.width;

  // 3. Per-frame BICUBIC resize (Pillow fixed-point).
  runtime::ExecutionActivity resize_progress(
      runtime::ExecutionActivityKind::kMediaResize);
  TimingScope resize_timing(options.timings, TimingStage::kResize);
  std::vector<std::vector<uint8_t>> resized(F);
  for (int f = 0; f < F; ++f) {
    if (!ProcessorContinue(options, err)) return false;
    resized[f].resize(static_cast<size_t>(rh) * rw * 3);
    if (rh != H || rw != W) {
      BicubicResize(decoded[f].data(), H, W, resized[f].data(), rh, rw);
    } else {
      resized[f] = decoded[f];
    }
    resize_progress.Completed();
  }

  // 4. Odd-frame pad: repeat the LAST frame until F is even.
  int Fp = (F % 2 == 0) ? F : F + 1;
  if (Fp != F) resized.push_back(resized[F - 1]);
  const int grid_t = Fp / cfg.temporal_patch_size;
  resize_timing.Stop();
  resize_progress.End();

  // 5. rescale + normalize (per frame, exact float32, same as ProcessImage).
  runtime::ExecutionActivity normalize_progress(
      runtime::ExecutionActivityKind::kMediaNormalize);
  TimingScope normalize_timing(options.timings, TimingStage::kNormalize);
  const double kInv255 = 1.0 / 255.0;
  std::vector<std::vector<float>> norm(Fp);
  for (int f = 0; f < Fp; ++f) {
    if (!ProcessorContinue(options, err)) return false;
    norm[f].resize(static_cast<size_t>(rh) * rw * 3);
    for (size_t i = 0; i < norm[f].size(); ++i) {
      const int c = static_cast<int>(i % 3);
      float fl = static_cast<float>(static_cast<double>(resized[f][i]) * kInv255);
      norm[f][i] = (fl - cfg.image_mean[c]) / cfg.image_std[c];
    }
    normalize_progress.Completed();
  }

  // 6. Temporal patchify: per-patch [C, T, P, P] with T real frames,
  //    block-major spatial within each time group (same layout as image).
  normalize_timing.Stop();
  normalize_progress.End();
  runtime::ExecutionActivity patchify_progress(
      runtime::ExecutionActivityKind::kMediaPatchify);
  TimingScope patchify_timing(options.timings, TimingStage::kPatchify);
  const int P = cfg.patch_size;
  const int T = cfg.temporal_patch_size;
  const int M = cfg.merge_size;
  const int gh = rh / P;
  const int gw = rw / P;
  const int patch_dim = 3 * T * P * P;
  const int L = grid_t * gh * gw;

  out->cfg = cfg;
  out->grid_t = grid_t;
  out->grid_h = gh;
  out->grid_w = gw;
  out->pixel_values.resize(static_cast<size_t>(L) * patch_dim);

  for (int g = 0; g < grid_t; ++g) {
    for (int hb = 0; hb < gh / M; ++hb) {
      for (int wb = 0; wb < gw / M; ++wb) {
        for (int mb = 0; mb < M; ++mb) {
          for (int mj = 0; mj < M; ++mj) {
            const int i = hb * M + mb;  // patch row
            const int j = wb * M + mj;  // patch col
            // Full index = time group offset (g * gh * gw) + block-major
            // spatial index within the group (same as the image case).
            const int p_idx =
                g * gh * gw + (hb * (gw / M) + wb) * (M * M) + mb * M + mj;
            float* dst =
                out->pixel_values.data() + static_cast<size_t>(p_idx) * patch_dim;
            for (int c = 0; c < 3; ++c) {
              for (int t = 0; t < T; ++t) {
                const int f = g * T + t;
                float* plane = dst + (c * T + t) * P * P;
                for (int p = 0; p < P; ++p) {
                  for (int q = 0; q < P; ++q) {
                    plane[p * P + q] =
                        norm[f][(static_cast<size_t>(i * P + p) * rw +
                                 (j * P + q)) * 3 + c];
                  }
                }
              }
            }
          }
        }
      }
    }
  }

  patchify_timing.Stop();
  patchify_progress.Finish();
  guard.success = ProcessorContinue(options, err);
  return guard.success;
}

}  // namespace vision
}  // namespace q4t
