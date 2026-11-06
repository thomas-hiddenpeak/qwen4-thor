#include "q4t/vision/media_plan.h"

#include <algorithm>
#include <array>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <limits>
#include <new>
#include <span>

namespace {
bool deny_allocations = false;
size_t allocation_attempts = 0;

bool Deny() noexcept {
  if (!deny_allocations) return false;
  ++allocation_attempts;
  return true;
}
}  // namespace

extern "C" void* __real_malloc(size_t);
extern "C" void* __real_realloc(void*, size_t);
extern "C" void* __real_calloc(size_t, size_t);
extern "C" void* __wrap_malloc(size_t size) {
  return Deny() ? nullptr : __real_malloc(size);
}
extern "C" void* __wrap_realloc(void* ptr, size_t size) {
  return Deny() ? nullptr : __real_realloc(ptr, size);
}
extern "C" void* __wrap_calloc(size_t count, size_t size) {
  return Deny() ? nullptr : __real_calloc(count, size);
}
void* operator new(size_t size) {
  if (Deny()) throw std::bad_alloc();
  if (void* p = __real_malloc(size ? size : 1)) return p;
  throw std::bad_alloc();
}
void* operator new[](size_t size) { return ::operator new(size); }
void operator delete(void* ptr) noexcept { std::free(ptr); }
void operator delete[](void* ptr) noexcept { std::free(ptr); }
void operator delete(void* ptr, size_t) noexcept { std::free(ptr); }
void operator delete[](void* ptr, size_t) noexcept { std::free(ptr); }

namespace {
using namespace q4t::vision;
using Error = MediaPlanError;

struct NoAllocation {
  NoAllocation() { deny_allocations = true; }
  ~NoAllocation() { deny_allocations = false; }
};

bool Check(bool condition, const char* why) {
  if (!condition) std::fprintf(stderr, "  FAIL: %s\n", why);
  return condition;
}

void Be32(uint8_t* out, uint32_t n) {
  out[0] = n >> 24;
  out[1] = n >> 16;
  out[2] = n >> 8;
  out[3] = n;
}

void RepairCrc(std::array<uint8_t, 33>* bytes) {
  uint32_t crc = 0xffffffffu;
  for (size_t i = 12; i < 29; ++i) {
    crc ^= (*bytes)[i];
    for (int bit = 0; bit < 8; ++bit) {
      crc = (crc >> 1) ^ (0xedb88320u & (0u - (crc & 1)));
    }
  }
  Be32(bytes->data() + 29, ~crc);
}

std::array<uint8_t, 33> Png(uint32_t width, uint32_t height) {
  std::array<uint8_t, 33> bytes = {137, 80, 78, 71, 13, 10, 26, 10,
                                   0,   0,  0,  13, 73, 72, 68, 82};
  Be32(bytes.data() + 16, width);
  Be32(bytes.data() + 20, height);
  bytes[24] = 8;
  bytes[25] = 2;
  RepairCrc(&bytes);
  return bytes;
}

// APP0 segment followed by an RGB SOF. This is metadata, not a full image.
std::array<uint8_t, 27> Jpeg(uint8_t marker = 0xc0) {
  return {0xff, 0xd8, 0xff, 0xe0, 0, 4,    0, 0, 0xff, marker, 0, 17,   8, 0,
          100,  0,    140,  3,    1, 0x11, 0, 2, 0x11, 0,      3, 0x11, 0};
}

bool Header(MediaBytes bytes, Error expected, uint32_t width = 0,
            uint32_t height = 0) {
  MediaHeader result{MediaFormat::kJpeg, 123, 456};
  MediaPlanResult status;
  {
    NoAllocation guard;
    status = ReadMediaHeader(bytes, &result);
  }
  return Check(status.error == expected, MediaPlanErrorCode(status.error)) &&
         Check(result.width == width && result.height == height,
               "header dimensions or transactional clear");
}

struct Fixture {
  std::array<uint8_t, 33> png = Png(140, 100);
  std::array<MediaBytes, 33> frames;
  std::array<MediaPlanInput, 9> items;
  ProcessorConfig image;
  ProcessorConfig video;
  MediaPlanLimits limits;
  MediaContextBudget context{100, 1, 8192, 208896};
  MediaDeviceBudget device{48 * 1024};  // Synthetic function capability.
  MediaPlan plan;
  size_t item_count = 1;

  Fixture() {
    frames.fill(png);
    for (auto& item : items) item = {MediaKind::kImage, {frames.data(), 1}};
    video.min_pixels = 4096;
    video.max_pixels = 25165824;
  }
  void Video(size_t count) {
    items[0] = {MediaKind::kVideo, {frames.data(), count}};
    context.placeholders = (count + 1) / 2;
  }
  bool Run(Error expected, size_t item = 0, size_t frame = 0) {
    plan.item_count = 7;
    plan.patches = 999;
    MediaPlanResult status;
    {
      NoAllocation guard;
      status = PlanMedia({items.data(), item_count}, image, video, limits,
                         context, device, &plan);
    }
    return Check(status.error == expected, MediaPlanErrorCode(status.error)) &&
           Check(status.item == item && status.frame == frame,
                 "error location") &&
           Check(status || (plan.item_count == 0 && plan.patches == 0 &&
                            plan.items[0].geometry.patches == 0),
                 "no partial plan on failure");
  }
};

bool Geometry(MediaKind kind, uint32_t width, uint32_t height, size_t frames,
              int rw, int rh, int gt, Error expected = Error::kNone,
              bool small_budget = false) {
  ProcessorConfig cfg;
  if (kind == MediaKind::kVideo) {
    cfg.min_pixels = 4096;
    cfg.max_pixels = 25165824;
  }
  if (small_budget) {
    cfg.min_pixels = 1024;
    cfg.max_pixels = 65536;
  }
  MediaGeometry out;
  MediaPlanResult status;
  {
    NoAllocation guard;
    status = PlanMediaGeometry(kind, width, height, frames, cfg, &out);
  }
  return Check(status.error == expected, MediaPlanErrorCode(status.error)) &&
         Check(out.width == rw && out.height == rh && out.grid_t == gt,
               "geometry or empty failure output");
}

struct Case {
  const char* name;
  bool (*run)();
};

const Case kCases[] = {
    {"png_rgb", [] { return Header(Png(140, 100), Error::kNone, 140, 100); }},
    {"png_bad_crc",
     [] {
       auto p = Png(1, 1);
       p[29] ^= 1;
       return Header(p, Error::kInvalidHeader);
     }},
    {"png_zero", [] { return Header(Png(0, 1), Error::kInvalidHeader); }},
    {"png_unsigned_edge",
     [] { return Header(Png(0xffffffffu, 1), Error::kInvalidHeader); }},
    {"png_bad_color_depth",
     [] {
       auto p = Png(1, 1);
       p[24] = 1;
       RepairCrc(&p);
       return Header(p, Error::kInvalidHeader);
     }},
    {"png_gray16_interlaced",
     [] {
       auto p = Png(2, 3);
       p[24] = 16;
       p[25] = 0;
       p[28] = 1;
       RepairCrc(&p);
       return Header(p, Error::kNone, 2, 3);
     }},
    {"png_invalid_method",
     [] {
       auto p = Png(1, 1);
       p[26] = 1;
       RepairCrc(&p);
       return Header(p, Error::kInvalidHeader);
     }},
    {"png_wrong_first_chunk",
     [] {
       auto p = Png(1, 1);
       p[12] = 'X';
       RepairCrc(&p);
       return Header(p, Error::kInvalidHeader);
     }},
    {"png_wrong_ihdr_length",
     [] {
       auto p = Png(1, 1);
       p[11] = 12;
       return Header(p, Error::kInvalidHeader);
     }},
    {"png_all_truncations",
     [] {
       auto p = Png(140, 100);
       for (size_t n = 0; n < p.size(); ++n) {
         if (!Header({p.data(), n},
                     n < 8 ? Error::kUnsupportedFormat : Error::kInvalidHeader))
           return false;
       }
       return true;
     }},
    {"jpeg_baseline", [] { return Header(Jpeg(), Error::kNone, 140, 100); }},
    {"jpeg_extended",
     [] { return Header(Jpeg(0xc1), Error::kNone, 140, 100); }},
    {"jpeg_progressive",
     [] { return Header(Jpeg(0xc2), Error::kNone, 140, 100); }},
    {"jpeg_lossless_rejected",
     [] { return Header(Jpeg(0xc3), Error::kUnsupportedFormat); }},
    {"jpeg_all_truncations",
     [] {
       auto p = Jpeg();
       for (size_t n = 0; n < p.size(); ++n) {
         if (!Header({p.data(), n},
                     n < 2 ? Error::kUnsupportedFormat : Error::kInvalidHeader))
           return false;
       }
       return true;
     }},
    {"jpeg_bad_length",
     [] {
       auto p = Jpeg();
       p[5] = 1;
       return Header(p, Error::kInvalidHeader);
     }},
    {"jpeg_early_scan",
     [] {
       auto p = Jpeg();
       p[3] = 0xda;
       return Header(p, Error::kInvalidHeader);
     }},
    {"jpeg_duplicate_component",
     [] {
       auto p = Jpeg();
       p[21] = 1;
       return Header(p, Error::kInvalidHeader);
     }},
    {"jpeg_invalid_sampling",
     [] {
       auto p = Jpeg();
       p[19] = 0x10;
       return Header(p, Error::kInvalidHeader);
     }},
    {"jpeg_unknown_precision",
     [] {
       auto p = Jpeg();
       p[12] = 12;
       return Header(p, Error::kInvalidHeader);
     }},
    {"jpeg_fill_and_tem",
     [] {
       auto p = Jpeg();
       std::array<uint8_t, 30> expanded{};
       std::copy(p.begin(), p.begin() + 2, expanded.begin());
       expanded[2] = 0xff;
       expanded[3] = 1;
       expanded[4] = 0xff;
       std::copy(p.begin() + 2, p.end(), expanded.begin() + 5);
       return Header(expanded, Error::kNone, 140, 100);
     }},
    {"jpeg_oversize_segment",
     [] {
       auto p = Jpeg();
       p[4] = 0xff;
       return Header(p, Error::kInvalidHeader);
     }},
    {"jpeg_empty_dimensions",
     [] {
       auto p = Jpeg();
       p[14] = 0;
       return Header(p, Error::kInvalidHeader);
     }},
    {"gif_rejected",
     [] {
       const uint8_t p[] = {'G', 'I', 'F', '8', '9', 'a'};
       return Header(p, Error::kUnsupportedFormat);
     }},
    {"image_reference_140x100",
     [] { return Geometry(MediaKind::kImage, 140, 100, 1, 320, 224, 1); }},
    {"image_ties_away",
     [] { return Geometry(MediaKind::kImage, 272, 272, 1, 256, 256, 1); }},
    {"image_one_pixel",
     [] { return Geometry(MediaKind::kImage, 1, 1, 1, 256, 256, 1); }},
    {"image_shrink",
     [] {
       return Geometry(MediaKind::kImage, 512, 512, 1, 256, 256, 1,
                       Error::kNone, true);
     }},
    {"video_reference_odd5",
     [] { return Geometry(MediaKind::kVideo, 320, 224, 5, 320, 224, 3); }},
    {"video_reference_bicubic",
     [] { return Geometry(MediaKind::kVideo, 321, 225, 4, 320, 224, 2); }},
    {"video_tiny_upscale",
     [] { return Geometry(MediaKind::kVideo, 1, 1, 2, 64, 64, 1); }},
    {"video_frame_product_widened",
     [] {
       return Geometry(MediaKind::kVideo, 16384, 8192, 32, 1248, 608, 16);
     }},
    {"geometry_zero",
     [] {
       return Geometry(MediaKind::kImage, 0, 10, 1, 0, 0, 0, Error::kGeometry);
     }},
    {"geometry_aspect",
     [] {
       return Geometry(MediaKind::kImage, 201, 1, 1, 0, 0, 0, Error::kGeometry);
     }},
    {"geometry_extreme_int",
     [] {
       return Geometry(MediaKind::kImage, 0x7fffffffu, 0x7fffffffu, 1, 0, 0, 0,
                       Error::kIntegerRange);
     }},
    {"geometry_extreme_uint",
     [] {
       return Geometry(MediaKind::kImage, 0xffffffffu, 1, 1, 0, 0, 0,
                       Error::kIntegerRange);
     }},
    {"geometry_missing_frame",
     [] {
       return Geometry(MediaKind::kVideo, 320, 224, 1, 0, 0, 0,
                       Error::kFrameCount);
     }},
    {"plan_normal_values",
     [] {
       Fixture f;
       return f.Run(Error::kNone) &&
              Check(f.plan.patches == 280 && f.plan.merged_tokens == 70 &&
                        f.plan.expanded_prompt_tokens == 169 &&
                        f.plan.encoded_bytes == 33 &&
                        f.plan.decoded_rgb_bytes == 42000 &&
                        f.plan.pixel_elements == 430080 &&
                        f.plan.pixel_bf16_bytes == 860160 &&
                        f.plan.output_bf16_bytes == 358400 &&
                        f.plan.workspace_bytes == 42415872,
                    "independent normal plan amounts");
     }},
    {"plan_zero_items",
     [] {
       Fixture f;
       f.item_count = 0;
       return f.Run(Error::kItemCount);
     }},
    {"plan_nine_items",
     [] {
       Fixture f;
       f.item_count = 9;
       return f.Run(Error::kItemCount);
     }},
    {"plan_eight_items",
     [] {
       Fixture f;
       f.item_count = 8;
       f.context.placeholders = 8;
       return f.Run(Error::kNone) &&
              Check(f.plan.merged_tokens == 560 &&
                        f.plan.decoded_rgb_bytes == 336000,
                    "all eight summed");
     }},
    {"plan_33_frames",
     [] {
       Fixture f;
       f.Video(33);
       return f.Run(Error::kFrameCount);
     }},
    {"plan_32_frames",
     [] {
       Fixture f;
       f.Video(32);
       return f.Run(Error::kNone) &&
              Check(f.plan.items[0].geometry.grid_t == 16, "32-frame groups");
     }},
    {"plan_frame_limit",
     [] {
       Fixture f;
       f.Video(3);
       f.limits.max_frames_per_item = 2;
       return f.Run(Error::kFrameCount);
     }},
    {"plan_source_edge",
     [] {
       Fixture f;
       f.limits.max_source_edge = 139;
       return f.Run(Error::kSourceDimensions);
     }},
    {"plan_source_pixels",
     [] {
       Fixture f;
       f.limits.max_source_pixels = 13999;
       return f.Run(Error::kSourceDimensions);
     }},
    {"plan_source_exact",
     [] {
       Fixture f;
       f.limits.max_source_edge = 140;
       f.limits.max_source_pixels = 14000;
       return f.Run(Error::kNone);
     }},
    {"plan_encoded_exact",
     [] {
       Fixture f;
       f.limits.max_encoded_bytes = 33;
       return f.Run(Error::kNone);
     }},
    {"plan_encoded_over",
     [] {
       Fixture f;
       f.limits.max_encoded_bytes = 32;
       return f.Run(Error::kEncodedBytes);
     }},
    {"plan_encoded_aggregate",
     [] {
       Fixture f;
       f.Video(2);
       f.limits.max_encoded_bytes = 65;
       return f.Run(Error::kEncodedBytes, 0, 1);
     }},
    {"plan_rgb_exact",
     [] {
       Fixture f;
       f.limits.max_decoded_rgb_bytes = 42000;
       return f.Run(Error::kNone);
     }},
    {"plan_rgb_aggregate",
     [] {
       Fixture f;
       f.Video(2);
       f.limits.max_decoded_rgb_bytes = 83999;
       return f.Run(Error::kDecodedBytes, 0, 1);
     }},
    {"plan_tail_mismatch",
     [] {
       Fixture f;
       auto p = Png(141, 100);
       f.frames[31] = p;
       f.Video(32);
       return f.Run(Error::kFrameDimensions, 0, 31);
     }},
    {"plan_final_item_bad",
     [] {
       Fixture f;
       f.item_count = 8;
       f.context.placeholders = 8;
       const uint8_t p[] = {0};
       f.frames[1] = p;
       f.items[7].frames = {f.frames.data() + 1, 1};
       return f.Run(Error::kUnsupportedFormat, 7);
     }},
    {"plan_patch_exact",
     [] {
       Fixture f;
       f.limits.max_patches = 280;
       return f.Run(Error::kNone);
     }},
    {"plan_patch_over",
     [] {
       Fixture f;
       f.limits.max_patches = 279;
       return f.Run(Error::kPatchCount);
     }},
    {"plan_patch_aggregate",
     [] {
       Fixture f;
       f.item_count = 2;
       f.context.placeholders = 2;
       f.limits.max_patches = 559;
       return f.Run(Error::kPatchCount, 1);
     }},
    {"plan_workspace_exact",
     [] {
       Fixture f;
       f.limits.max_workspace_bytes = 42415872;
       return f.Run(Error::kNone);
     }},
    {"plan_workspace_over",
     [] {
       Fixture f;
       f.limits.max_workspace_bytes = 42415871;
       return f.Run(Error::kWorkspaceBytes);
     }},
    {"plan_device_unknown",
     [] {
       Fixture f;
       f.device.attention_shared_bytes = 0;
       return f.Run(Error::kDeviceLimitUnknown);
     }},
    {"plan_shared_exact",
     [] {
       Fixture f;
       f.device.attention_shared_bytes = 1120;
       return f.Run(Error::kNone);
     }},
    {"plan_shared_over",
     [] {
       Fixture f;
       f.device.attention_shared_bytes = 1119;
       return f.Run(Error::kAttentionSharedBytes);
     }},
    {"plan_shared_spatial_not_time",
     [] {
       Fixture f;
       f.png = Png(320, 224);
       f.Video(5);
       f.device.attention_shared_bytes = 1120;
       return f.Run(Error::kNone) &&
              Check(f.plan.patches == 840,
                    "temporal groups counted separately");
     }},
    {"plan_placeholder_mismatch",
     [] {
       Fixture f;
       f.context.placeholders = 2;
       return f.Run(Error::kPlaceholderCount);
     }},
    {"plan_prompt_underflow",
     [] {
       Fixture f;
       f.context.prompt_tokens_with_placeholders = 0;
       return f.Run(Error::kPlaceholderCount);
     }},
    {"plan_context_one_left",
     [] {
       Fixture f;
       f.context.max_sequence = 170;
       return f.Run(Error::kNone);
     }},
    {"plan_context_full",
     [] {
       Fixture f;
       f.context.max_sequence = 169;
       return f.Run(Error::kContextLength);
     }},
    {"plan_context_overflow",
     [] {
       Fixture f;
       f.context.prompt_tokens_with_placeholders = UINT64_MAX;
       return f.Run(Error::kContextLength);
     }},
    {"plan_prefill_exact",
     [] {
       Fixture f;
       f.context.max_prefill = 169;
       return f.Run(Error::kNone);
     }},
    {"plan_prefill_over",
     [] {
       Fixture f;
       f.context.max_prefill = 168;
       return f.Run(Error::kPrefillLength);
     }},
    {"plan_zero_limit_not_unlimited",
     [] {
       Fixture f;
       f.limits.max_patches = 0;
       return f.Run(Error::kInvalidConfig);
     }},
    {"plan_each_zero_budget",
     [] {
       constexpr uint64_t MediaPlanLimits::*fields[] = {
           &MediaPlanLimits::max_encoded_bytes,
           &MediaPlanLimits::max_source_edge,
           &MediaPlanLimits::max_source_pixels,
           &MediaPlanLimits::max_decoded_rgb_bytes,
           &MediaPlanLimits::max_patches,
           &MediaPlanLimits::max_workspace_bytes};
       for (auto field : fields) {
         Fixture f;
         f.limits.*field = 0;
         if (!f.Run(Error::kInvalidConfig)) return false;
       }
       Fixture f;
       f.limits.max_items = 0;
       if (!f.Run(Error::kInvalidConfig)) return false;
       f.limits.max_items = 8;
       f.limits.max_frames_per_item = 0;
       return f.Run(Error::kInvalidConfig);
     }},
    {"plan_invalid_kind",
     [] {
       Fixture f;
       f.items[0].kind = static_cast<MediaKind>(99);
       return f.Run(Error::kInvalidConfig);
     }},
    {"plan_empty_encoded",
     [] {
       Fixture f;
       f.frames[0] = {};
       return f.Run(Error::kEncodedBytes);
     }},
    {"plan_unknown_config",
     [] {
       Fixture f;
       f.video.temporal_patch_size = 3;
       return f.Run(Error::kInvalidConfig);
     }},
    {"plan_bad_normalization",
     [] {
       Fixture f;
       f.image.image_std[0] = 0;
       return f.Run(Error::kInvalidConfig);
     }},
    {"plan_bad_patch_config",
     [] {
       Fixture f;
       f.image.patch_size = INT32_MAX;
       return f.Run(Error::kInvalidConfig);
     }},
    {"plan_cuda_offset_range",
     [] {
       Fixture f;
       f.device.hidden_size = INT32_MAX;
       return f.Run(Error::kIntegerRange);
     }},
    {"plan_resample_index_range",
     [] {
       Fixture f;
       f.png = Png(30000, 30000);
       f.limits.max_source_edge = 30000;
       f.limits.max_source_pixels = 900000000;
       f.limits.max_decoded_rgb_bytes = UINT64_MAX;
       return f.Run(Error::kIntegerRange);
     }},
    {"null_outputs",
     [] {
       Fixture f;
       NoAllocation guard;
       return ReadMediaHeader(f.png, nullptr).error == Error::kInvalidConfig &&
              PlanMediaGeometry(MediaKind::kImage, 1, 1, 1, f.image, nullptr)
                      .error == Error::kInvalidConfig &&
              PlanMedia({f.items.data(), 1}, f.image, f.video, f.limits,
                        f.context, f.device, nullptr)
                      .error == Error::kInvalidConfig;
     }},
};
}  // namespace

int main(int argc, char** argv) {
  size_t total = 0, passed = 0;
  for (const auto& item : kCases) {
    if (argc == 2 && std::strcmp(argv[1], item.name) != 0) continue;
    ++total;
    const size_t before = allocation_attempts;
    const bool okay = item.run() && Check(allocation_attempts == before,
                                          "planner attempted allocation");
    if (okay) ++passed;
    std::printf("%s %s\n", okay ? "PASS" : "FAIL", item.name);
  }
  std::printf("%zu cases, %zu passed, %zu failed; %zu allocation attempts\n",
              total, passed, total - passed, allocation_attempts);
  return total && total == passed ? 0 : 1;
}
