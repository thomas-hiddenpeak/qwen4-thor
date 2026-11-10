#include "q4t/vision/media_plan.h"

#include <algorithm>
#include <cmath>
#include <limits>

namespace q4t::vision {
namespace {

constexpr uint64_t kIntMax = std::numeric_limits<int>::max();
constexpr uint64_t kU64Max = std::numeric_limits<uint64_t>::max();
// CUDA ceil-div expressions add kBlock - 1 before dividing. Leave that room.
constexpr uint64_t kKernelElements = kIntMax - 255;

MediaPlanResult Fail(MediaPlanError error, size_t item = 0,
                     size_t frame = 0) noexcept {
  return {error, item, frame};
}

bool Add(uint64_t a, uint64_t b, uint64_t* out) noexcept {
  if (b > kU64Max - a) return false;
  *out = a + b;
  return true;
}

bool Mul(uint64_t a, uint64_t b, uint64_t* out) noexcept {
  if (a && b > kU64Max / a) return false;
  *out = a * b;
  return true;
}

uint32_t Be32(const uint8_t* p) noexcept {
  return (uint32_t{p[0]} << 24) | (uint32_t{p[1]} << 16) |
         (uint32_t{p[2]} << 8) | p[3];
}

uint32_t Be16(const uint8_t* p) noexcept {
  return (uint32_t{p[0]} << 8) | p[1];
}

uint32_t PngCrc(std::span<const uint8_t> bytes) noexcept {
  uint32_t crc = 0xffffffffu;
  for (uint8_t byte : bytes) {
    crc ^= byte;
    for (int i = 0; i < 8; ++i) {
      crc = (crc >> 1) ^ ((crc & 1) ? 0xedb88320u : 0);
    }
  }
  return crc ^ 0xffffffffu;
}

bool ValidConfig(const ProcessorConfig& config) noexcept {
  if (config.patch_size <= 0 ||
      config.patch_size > std::numeric_limits<int>::max() / 2 ||
      config.merge_size != 2 || config.temporal_patch_size != 2 ||
      config.min_pixels <= 0 || config.max_pixels < config.min_pixels) {
    return false;
  }
  for (int i = 0; i < 3; ++i) {
    if (!std::isfinite(config.image_mean[i]) ||
        !std::isfinite(config.image_std[i]) || config.image_std[i] <= 0) {
      return false;
    }
  }
  return true;
}

// The rounded quotient must be representable BEFORE narrowing to int.
bool Aligned(double units, int factor, int* out) noexcept {
  if (!std::isfinite(units) || units < 0 || units > kIntMax / factor) {
    return false;
  }
  *out = static_cast<int>(units) * factor;
  return true;
}

bool ResampleIndicesFit(int in_size, int out_size) noexcept {
  const double scale = std::max(1.0, static_cast<double>(in_size) / out_size);
  const double kernel_size = std::ceil(2 * scale) * 2 + 1;
  return kernel_size <= kKernelElements &&
         static_cast<uint64_t>(out_size) * static_cast<uint64_t>(kernel_size) <=
             kKernelElements &&
         static_cast<uint64_t>(out_size) * 2 <= kKernelElements;
}

}  // namespace

const char* MediaPlanErrorCode(MediaPlanError error) noexcept {
  switch (error) {
    case MediaPlanError::kNone:
      return "ok";
    case MediaPlanError::kInvalidConfig:
      return "media_config_invalid";
    case MediaPlanError::kItemCount:
      return "media_item_limit";
    case MediaPlanError::kFrameCount:
      return "media_frame_limit";
    case MediaPlanError::kEncodedBytes:
      return "media_encoded_limit";
    case MediaPlanError::kUnsupportedFormat:
      return "media_format_unsupported";
    case MediaPlanError::kInvalidHeader:
      return "media_header_invalid";
    case MediaPlanError::kSourceDimensions:
      return "media_source_limit";
    case MediaPlanError::kFrameDimensions:
      return "media_frame_dimensions";
    case MediaPlanError::kDecodedBytes:
      return "media_decoded_limit";
    case MediaPlanError::kGeometry:
      return "media_geometry_invalid";
    case MediaPlanError::kDetailLimit:
      return "media_detail_limit";
    case MediaPlanError::kIntegerRange:
      return "media_integer_range";
    case MediaPlanError::kPatchCount:
      return "media_patch_limit";
    case MediaPlanError::kWorkspaceBytes:
      return "media_workspace_limit";
    case MediaPlanError::kDeviceLimitUnknown:
      return "media_device_limit_unknown";
    case MediaPlanError::kAttentionSharedBytes:
      return "media_attention_limit";
    case MediaPlanError::kPlaceholderCount:
      return "media_placeholder_count";
    case MediaPlanError::kContextLength:
      return "context_length_exceeded";
    case MediaPlanError::kPrefillLength:
      return "media_prefill_limit";
  }
  return "media_config_invalid";
}

MediaPlanResult ReadMediaHeader(std::span<const uint8_t> bytes,
                                MediaHeader* out) noexcept {
  if (!out) return Fail(MediaPlanError::kInvalidConfig);
  *out = {};
  constexpr uint8_t kPng[] = {137, 80, 78, 71, 13, 10, 26, 10};
  if (bytes.size() >= 8 &&
      std::equal(std::begin(kPng), std::end(kPng), bytes.begin())) {
    if (bytes.size() < 33 || Be32(bytes.data() + 8) != 13 ||
        Be32(bytes.data() + 12) != 0x49484452 ||
        PngCrc(bytes.subspan(12, 17)) != Be32(bytes.data() + 29)) {
      return Fail(MediaPlanError::kInvalidHeader);
    }
    const uint32_t width = Be32(bytes.data() + 16);
    const uint32_t height = Be32(bytes.data() + 20);
    const uint8_t depth = bytes[24], color = bytes[25];
    const bool legal_depth =
        (color == 0 && (depth == 1 || depth == 2 || depth == 4 || depth == 8 ||
                        depth == 16)) ||
        (color == 3 &&
         (depth == 1 || depth == 2 || depth == 4 || depth == 8)) ||
        ((color == 2 || color == 4 || color == 6) &&
         (depth == 8 || depth == 16));
    if (!width || !height || width > kIntMax || height > kIntMax ||
        !legal_depth || bytes[26] != 0 || bytes[27] != 0 || bytes[28] > 1) {
      return Fail(MediaPlanError::kInvalidHeader);
    }
    *out = {MediaFormat::kPng, width, height};
    return {};
  }
  if (bytes.size() < 2 || bytes[0] != 0xff || bytes[1] != 0xd8) {
    return Fail(MediaPlanError::kUnsupportedFormat);
  }
  size_t pos = 2;
  while (pos < bytes.size()) {
    if (bytes[pos++] != 0xff) return Fail(MediaPlanError::kInvalidHeader);
    while (pos < bytes.size() && bytes[pos] == 0xff) ++pos;
    if (pos == bytes.size()) return Fail(MediaPlanError::kInvalidHeader);
    const uint8_t marker = bytes[pos++];
    if (marker == 0x01) continue;  // Standalone TEM, no segment length.
    if (marker == 0 || (marker >= 0xd0 && marker <= 0xda) ||
        bytes.size() - pos < 2) {
      return Fail(MediaPlanError::kInvalidHeader);
    }
    const uint32_t length = Be16(bytes.data() + pos);
    if (length < 2 || length > bytes.size() - pos) {
      return Fail(MediaPlanError::kInvalidHeader);
    }
    const bool sof = marker >= 0xc0 && marker <= 0xcf && marker != 0xc4 &&
                     marker != 0xc8 && marker != 0xcc;
    if (sof) {
      if (marker != 0xc0 && marker != 0xc1 && marker != 0xc2) {
        return Fail(MediaPlanError::kUnsupportedFormat);
      }
      if (length < 8 || bytes[pos + 2] != 8) {
        return Fail(MediaPlanError::kInvalidHeader);
      }
      const uint32_t height = Be16(bytes.data() + pos + 3);
      const uint32_t width = Be16(bytes.data() + pos + 5);
      const uint8_t components = bytes[pos + 7];
      if (!width || !height ||
          (components != 1 && components != 3 && components != 4) ||
          length != 8u + 3u * components) {
        return Fail(MediaPlanError::kInvalidHeader);
      }
      for (size_t i = 0; i < components; ++i) {
        const uint8_t sampling = bytes[pos + 9 + 3 * i];
        if ((sampling >> 4) < 1 || (sampling >> 4) > 4 || (sampling & 15) < 1 ||
            (sampling & 15) > 4 || bytes[pos + 10 + 3 * i] > 3) {
          return Fail(MediaPlanError::kInvalidHeader);
        }
        for (size_t j = 0; j < i; ++j) {
          if (bytes[pos + 8 + 3 * i] == bytes[pos + 8 + 3 * j]) {
            return Fail(MediaPlanError::kInvalidHeader);
          }
        }
      }
      *out = {MediaFormat::kJpeg, width, height};
      return {};
    }
    pos += length;
  }
  return Fail(MediaPlanError::kInvalidHeader);
}

MediaPlanResult PlanMediaGeometry(MediaKind kind, uint32_t width,
                                  uint32_t height, size_t frames,
                                  const ProcessorConfig& config,
                                  MediaGeometry* out,
                                  ImageDetail detail) noexcept {
  if (!out) return Fail(MediaPlanError::kInvalidConfig);
  *out = {};
  if (!ValidConfig(config) ||
      (kind != MediaKind::kImage && kind != MediaKind::kVideo)) {
    return Fail(MediaPlanError::kInvalidConfig);
  }
  const bool video = kind == MediaKind::kVideo;
  if ((detail != ImageDetail::kAuto && detail != ImageDetail::kLow &&
       detail != ImageDetail::kHigh && detail != ImageDetail::kOriginal) ||
      (video && detail != ImageDetail::kAuto))
    return Fail(MediaPlanError::kInvalidConfig);
  if ((!video && frames != 1) || (video && frames < 2) ||
      frames > kMediaPlanFrames) {
    return Fail(MediaPlanError::kFrameCount);
  }
  if (!height || !width) return Fail(MediaPlanError::kGeometry);
  if (height > kIntMax || width > kIntMax) {
    return Fail(MediaPlanError::kIntegerRange);
  }
  const int factor = config.patch_size * config.merge_size;
  int h = static_cast<int>(height), w = static_cast<int>(width);
  if (video && (h < factor || w < factor)) {
    const double scale = std::max(static_cast<double>(factor) / h,
                                  static_cast<double>(factor) / w);
    const double scaled_h = h * scale, scaled_w = w * scale;
    if (scaled_h > kIntMax || scaled_w > kIntMax) {
      return Fail(MediaPlanError::kIntegerRange);
    }
    h = static_cast<int>(scaled_h);
    w = static_cast<int>(scaled_w);
  }
  if (static_cast<double>(std::max(h, w)) / std::min(h, w) > 200.0) {
    return Fail(MediaPlanError::kGeometry);
  }
  int rh = 0, rw = 0;
  // Python round uses ties-to-even. Integer division avoids floating-point
  // environment dependence and differs from std::round at half factors.
  const auto round_even = [](uint64_t value, uint64_t divisor) {
    const uint64_t quotient = value / divisor;
    const uint64_t twice_remainder = 2 * (value % divisor);
    return quotient + (twice_remainder > divisor ||
                        (twice_remainder == divisor && (quotient & 1)));
  };
  if (detail == ImageDetail::kOriginal) {
    if (!Aligned((uint64_t{height} + factor - 1) / factor, factor, &rh) ||
        !Aligned((uint64_t{width} + factor - 1) / factor, factor, &rw))
      return Fail(MediaPlanError::kIntegerRange);
    if (static_cast<uint64_t>(rh) * rw >
        static_cast<uint64_t>(config.max_pixels))
      return Fail(MediaPlanError::kDetailLimit);
  } else {
    if (!Aligned(round_even(h, factor), factor, &rh) ||
        !Aligned(round_even(w, factor), factor, &rw)) {
      return Fail(MediaPlanError::kIntegerRange);
    }
    // The reference resizes before padding; its budget rounds to nearest even
    // temporal groups, while patchification below always pads the last frame.
    const uint64_t budget_frames = video ? round_even(frames, 2) * 2 : 1;
    uint64_t rounded_pixels = 0, source_pixels = 0;
    if (!Mul(rh, rw, &rounded_pixels) ||
        !Mul(rounded_pixels, budget_frames, &rounded_pixels) ||
        !Mul(h, w, &source_pixels) ||
        !Mul(source_pixels, video ? frames : 1, &source_pixels)) {
      return Fail(MediaPlanError::kIntegerRange);
    }
    if (rounded_pixels > static_cast<uint64_t>(config.max_pixels)) {
      const double beta =
          std::sqrt(static_cast<double>(source_pixels) / config.max_pixels);
      if (!Aligned(std::max(1.0, std::floor(h / beta / factor)), factor, &rh) ||
          !Aligned(std::max(1.0, std::floor(w / beta / factor)), factor, &rw)) {
        return Fail(MediaPlanError::kIntegerRange);
      }
    } else if (rounded_pixels < static_cast<uint64_t>(config.min_pixels)) {
      const double beta = std::sqrt(static_cast<double>(config.min_pixels) /
                                    static_cast<double>(source_pixels));
      if (!Aligned(std::ceil(h * beta / factor), factor, &rh) ||
          !Aligned(std::ceil(w * beta / factor), factor, &rw)) {
        return Fail(MediaPlanError::kIntegerRange);
      }
    }
    if (detail == ImageDetail::kLow || detail == ImageDetail::kHigh) {
      const int edge = detail == ImageDetail::kLow ? 512 : 2048;
      const int pixels = std::min(config.max_pixels, edge * edge);
      if (factor > edge || static_cast<uint64_t>(factor) * factor >
                               static_cast<uint64_t>(pixels))
        return Fail(MediaPlanError::kDetailLimit);
      // Additional detail policy after the unchanged reference smart-resize.
      // Floor to patch alignment so rounding cannot exceed either ceiling.
      const double scale =
          std::min({1.0, static_cast<double>(edge) / std::max(rh, rw),
                    std::sqrt(static_cast<double>(pixels) /
                              (static_cast<uint64_t>(rh) * rw))});
      if (!Aligned(std::max(1.0, std::floor(rh * scale / factor)), factor,
                   &rh) ||
          !Aligned(std::max(1.0, std::floor(rw * scale / factor)), factor, &rw))
        return Fail(MediaPlanError::kIntegerRange);
      if (rh > edge || rw > edge ||
          static_cast<uint64_t>(rh) * rw > static_cast<uint64_t>(pixels))
        return Fail(MediaPlanError::kDetailLimit);
    }
  }
  if (rh <= 0 || rw <= 0) return Fail(MediaPlanError::kGeometry);
  // Current resampler uses int row offsets and coefficient indices. Its
  // horizontal intermediate is source_height * resized_width (not rh * rw).
  if (static_cast<uint64_t>(height) * width * 3 > kKernelElements ||
      static_cast<uint64_t>(height) * rw * 3 > kKernelElements ||
      static_cast<uint64_t>(rh) * rw * 3 > kKernelElements ||
      !ResampleIndicesFit(static_cast<int>(width), rw) ||
      !ResampleIndicesFit(static_cast<int>(height), rh)) {
    return Fail(MediaPlanError::kIntegerRange);
  }
  MediaGeometry result;
  result.width = rw;
  result.height = rh;
  result.padded_frames = video ? static_cast<int>(((frames + 1) / 2) * 2) : 2;
  result.grid_t = result.padded_frames / 2;
  result.grid_h = rh / config.patch_size;
  result.grid_w = rw / config.patch_size;
  uint64_t spatial = 0, patch_dim = 0;
  if (!Mul(result.grid_h, result.grid_w, &spatial) ||
      !Mul(spatial, result.grid_t, &result.patches) ||
      !Mul(config.patch_size, config.patch_size, &patch_dim) ||
      !Mul(patch_dim, 6, &patch_dim) ||
      !Mul(result.patches, patch_dim, &result.pixel_elements) ||
      !Mul(spatial, sizeof(float), &result.attention_shared_bytes) ||
      result.patches > kKernelElements || patch_dim > kKernelElements ||
      result.pixel_elements > kKernelElements ||
      result.attention_shared_bytes > kKernelElements) {
    return Fail(MediaPlanError::kIntegerRange);
  }
  result.merged_tokens = result.patches / 4;
  *out = result;
  return {};
}

MediaPlanResult PlanMediaSources(std::span<const MediaPlanInput> inputs,
                                 const ProcessorConfig& image_config,
                                 const ProcessorConfig& video_config,
                                 const MediaPlanLimits& limits,
                                 MediaPlan* out) noexcept {
  if (!out) return Fail(MediaPlanError::kInvalidConfig);
  *out = {};
  if (!ValidConfig(image_config) || !ValidConfig(video_config) ||
      image_config.patch_size != video_config.patch_size || !limits.max_items ||
      limits.max_items > kMediaPlanItems || !limits.max_frames_per_item ||
      limits.max_frames_per_item > kMediaPlanFrames ||
      !limits.max_encoded_bytes || !limits.max_source_edge ||
      !limits.max_source_pixels || !limits.max_decoded_rgb_bytes ||
      !limits.max_patches || !limits.max_workspace_bytes) {
    return Fail(MediaPlanError::kInvalidConfig);
  }
  if (inputs.empty() || inputs.size() > limits.max_items) {
    return Fail(MediaPlanError::kItemCount);
  }
  MediaPlan plan;
  for (size_t i = 0; i < inputs.size(); ++i) {
    const auto& input = inputs[i];
    if (input.kind != MediaKind::kImage && input.kind != MediaKind::kVideo) {
      return Fail(MediaPlanError::kInvalidConfig, i);
    }
    if (input.frames.size() > limits.max_frames_per_item ||
        (input.kind == MediaKind::kImage && input.frames.size() != 1) ||
        (input.kind == MediaKind::kVideo && input.frames.size() < 2)) {
      return Fail(MediaPlanError::kFrameCount, i);
    }
    MediaItemPlan item;
    item.kind = input.kind;
    item.detail = input.detail;
    item.frames = input.frames.size();
    for (size_t f = 0; f < input.frames.size(); ++f) {
      const auto bytes = input.frames[f];
      if (bytes.empty() || bytes.size() > kIntMax ||
          !Add(plan.encoded_bytes, bytes.size(), &plan.encoded_bytes) ||
          plan.encoded_bytes > limits.max_encoded_bytes) {
        return Fail(MediaPlanError::kEncodedBytes, i, f);
      }
      MediaHeader header;
      const auto read = ReadMediaHeader(bytes, &header);
      if (!read) return Fail(read.error, i, f);
      const uint64_t pixels = uint64_t{header.width} * header.height;
      if (header.width > limits.max_source_edge ||
          header.height > limits.max_source_edge ||
          pixels > limits.max_source_pixels) {
        return Fail(MediaPlanError::kSourceDimensions, i, f);
      }
      // Current processor resample row offsets/strides use signed int.
      if (pixels > kKernelElements / 3) {
        return Fail(MediaPlanError::kIntegerRange, i, f);
      }
      if (!Add(plan.decoded_rgb_bytes, pixels * 3, &plan.decoded_rgb_bytes) ||
          plan.decoded_rgb_bytes > limits.max_decoded_rgb_bytes) {
        return Fail(MediaPlanError::kDecodedBytes, i, f);
      }
      if (f && (header.width != item.source_width ||
                header.height != item.source_height)) {
        return Fail(MediaPlanError::kFrameDimensions, i, f);
      }
      item.source_width = header.width;
      item.source_height = header.height;
    }
    const auto geometry = PlanMediaGeometry(
        item.kind, item.source_width, item.source_height, item.frames,
        item.kind == MediaKind::kImage ? image_config : video_config,
        &item.geometry, item.detail);
    if (!geometry) return Fail(geometry.error, i);
    if (!Add(plan.patches, item.geometry.patches, &plan.patches) ||
        plan.patches > limits.max_patches) {
      return Fail(MediaPlanError::kPatchCount, i);
    }
    if (!Add(plan.merged_tokens, item.geometry.merged_tokens,
             &plan.merged_tokens) ||
        !Add(plan.pixel_elements, item.geometry.pixel_elements,
             &plan.pixel_elements) ||
        plan.pixel_elements > kKernelElements) {
      return Fail(MediaPlanError::kIntegerRange, i);
    }
    plan.items[i] = item;
  }
  plan.item_count = inputs.size();
  *out = plan;
  return {};
}

MediaPlanResult PlanMedia(std::span<const MediaPlanInput> inputs,
                          const ProcessorConfig& image_config,
                          const ProcessorConfig& video_config,
                          const MediaPlanLimits& limits,
                          const MediaContextBudget& context,
                          const MediaDeviceBudget& device,
                          MediaPlan* out) noexcept {
  if (!out) return Fail(MediaPlanError::kInvalidConfig);
  *out = {};
  if (!context.max_prefill || context.max_sequence < 2 ||
      context.max_prefill > kIntMax || context.max_sequence > kIntMax ||
      device.hidden_size <= 0 || device.intermediate_size <= 0 ||
      device.out_hidden_size <= 0) {
    return Fail(MediaPlanError::kInvalidConfig);
  }
  // Source policy and all frames are checked without decoder/device activity.
  MediaPlan plan;
  const auto sources =
      PlanMediaSources(inputs, image_config, video_config, limits, &plan);
  if (!sources) return sources;
  size_t placeholders = 0;
  for (size_t i = 0; i < plan.item_count; ++i)
    placeholders += static_cast<size_t>(plan.items[i].geometry.grid_t);
  if (context.placeholders != placeholders ||
      context.prompt_tokens_with_placeholders < context.placeholders) {
    return Fail(MediaPlanError::kPlaceholderCount);
  }
  if (!device.attention_shared_bytes) {
    return Fail(MediaPlanError::kDeviceLimitUnknown);
  }
  for (size_t i = 0; i < plan.item_count; ++i) {
    if (plan.items[i].geometry.attention_shared_bytes >
        device.attention_shared_bytes) {
      return Fail(MediaPlanError::kAttentionSharedBytes, i);
    }
  }
  // Bound signed element offsets/ceil-div used by the current CUDA kernels.
  uint64_t count = 0;
  for (uint64_t dimension : {static_cast<uint64_t>(device.hidden_size) * 3,
                             static_cast<uint64_t>(device.intermediate_size)}) {
    if (!Mul(plan.patches, dimension, &count) || count > kKernelElements) {
      return Fail(MediaPlanError::kIntegerRange);
    }
  }
  if (!Mul(plan.merged_tokens, device.out_hidden_size, &count) ||
      count > kKernelElements ||
      !Mul(count, sizeof(uint16_t), &plan.output_bf16_bytes) ||
      !Mul(plan.pixel_elements, sizeof(uint16_t), &plan.pixel_bf16_bytes)) {
    return Fail(MediaPlanError::kIntegerRange);
  }
  const uint64_t stride =
      static_cast<uint64_t>(device.hidden_size) * 10 + device.intermediate_size;
  if (!Mul(plan.patches, stride, &plan.workspace_bytes) ||
      !Mul(plan.workspace_bytes, sizeof(uint16_t), &plan.workspace_bytes) ||
      !Add(plan.workspace_bytes, 32ull * 1024 * 1024, &plan.workspace_bytes) ||
      plan.workspace_bytes > limits.max_workspace_bytes) {
    return Fail(MediaPlanError::kWorkspaceBytes);
  }
  if (!Add(context.prompt_tokens_with_placeholders - context.placeholders,
           plan.merged_tokens, &plan.expanded_prompt_tokens) ||
      plan.expanded_prompt_tokens >= context.max_sequence) {
    return Fail(MediaPlanError::kContextLength);
  }
  if (plan.expanded_prompt_tokens > context.max_prefill) {
    return Fail(MediaPlanError::kPrefillLength);
  }
  plan.item_count = inputs.size();
  *out = plan;
  return {};
}

}  // namespace q4t::vision
