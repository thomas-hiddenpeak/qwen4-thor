// Allocation-free admission planning. No decoder, CUDA, or HTTP dependency.
// A successful header/plan is NOT proof that compressed pixels will decode,
// nor a physical RAM reservation. Recheck decoded dimensions before use.
#pragma once

#include <array>
#include <cstddef>
#include <cstdint>
#include <span>

#include "q4t/vision/processor.h"

namespace q4t::vision {

inline constexpr size_t kMediaPlanItems = 8;
inline constexpr size_t kMediaPlanFrames = 32;

enum class MediaKind { kImage, kVideo };
enum class MediaFormat { kPng, kJpeg };
enum class MediaPlanError {
  kNone,
  kInvalidConfig,
  kItemCount,
  kFrameCount,
  kEncodedBytes,
  kUnsupportedFormat,
  kInvalidHeader,
  kSourceDimensions,
  kFrameDimensions,
  kDecodedBytes,
  kGeometry,
  kDetailLimit,
  kIntegerRange,
  kPatchCount,
  kWorkspaceBytes,
  kDeviceLimitUnknown,
  kAttentionSharedBytes,
  kPlaceholderCount,
  kContextLength,
  kPrefillLength,
};

struct MediaPlanResult {
  MediaPlanError error = MediaPlanError::kNone;
  size_t item = 0;
  size_t frame = 0;
  explicit operator bool() const noexcept {
    return error == MediaPlanError::kNone;
  }
};

// Static strings; suitable for error paths even when allocation is impossible.
const char* MediaPlanErrorCode(MediaPlanError error) noexcept;

struct MediaHeader {
  MediaFormat format = MediaFormat::kPng;
  uint32_t width = 0;
  uint32_t height = 0;
};

// Metadata only. PNG includes IHDR CRC/field validation. JPEG walks bounded
// segments to an 8-bit C0/C1/C2 SOF; entropy/remaining chunks are not decoded.
MediaPlanResult ReadMediaHeader(std::span<const uint8_t> bytes,
                                MediaHeader* out) noexcept;

struct MediaGeometry {
  int width = 0;
  int height = 0;
  int padded_frames = 0;
  int grid_t = 0;
  int grid_h = 0;
  int grid_w = 0;
  uint64_t patches = 0;
  uint64_t merged_tokens = 0;
  uint64_t pixel_elements = 0;
  uint64_t attention_shared_bytes = 0;
};

// Auto preserves reference ties-to-even rounding and temporal padding.
// Low/high additionally bound the longest edge to 512/2048 and area to its
// square. Original preserves pixels with bottom/right edge padding; it fails
// when the padded image exceeds the configured pixel budget, never resizes.
MediaPlanResult PlanMediaGeometry(
    MediaKind kind, uint32_t width, uint32_t height, size_t frames,
    const ProcessorConfig& config, MediaGeometry* out,
    ImageDetail detail = ImageDetail::kAuto) noexcept;

using MediaBytes = std::span<const uint8_t>;
struct MediaPlanInput {
  MediaKind kind = MediaKind::kImage;
  std::span<const MediaBytes> frames;
  ImageDetail detail = ImageDetail::kAuto;
};

struct MediaPlanLimits {
  size_t max_items = kMediaPlanItems;
  size_t max_frames_per_item = kMediaPlanFrames;
  uint64_t max_encoded_bytes = 16ull * 1024 * 1024;
  uint64_t max_source_edge = 16384;
  uint64_t max_source_pixels = 16777216;
  // Sum of forced RGB source buffers, excluding decoder internal allocations.
  uint64_t max_decoded_rgb_bytes = 128ull * 1024 * 1024;
  uint64_t max_patches = 65536;
  uint64_t max_workspace_bytes = 2ull * 1024 * 1024 * 1024;
};

struct MediaContextBudget {
  uint64_t prompt_tokens_with_placeholders = 0;
  size_t placeholders = 0;  // One per image or video temporal patch group.
  uint64_t max_prefill = 0;
  uint64_t max_sequence = 0;  // Prompt must leave at least one output slot.
};

struct MediaDeviceBudget {
  // Caller must query the effective limit for the actual AttentionKernel.
  // Zero is unknown and fails closed; no assumed architecture default.
  uint64_t attention_shared_bytes = 0;
  int hidden_size = 1152;
  int intermediate_size = 4304;
  int out_hidden_size = 2560;
};

struct MediaItemPlan {
  MediaKind kind = MediaKind::kImage;
  uint32_t source_width = 0;
  uint32_t source_height = 0;
  size_t frames = 0;
  MediaGeometry geometry;
  ImageDetail detail = ImageDetail::kAuto;
};

struct MediaPlan {
  std::array<MediaItemPlan, kMediaPlanItems> items{};
  size_t item_count = 0;
  uint64_t encoded_bytes = 0;
  uint64_t decoded_rgb_bytes = 0;
  uint64_t patches = 0;
  uint64_t merged_tokens = 0;
  uint64_t expanded_prompt_tokens = 0;
  uint64_t pixel_elements = 0;
  uint64_t pixel_bf16_bytes = 0;
  uint64_t output_bf16_bytes = 0;
  // Actual trunk workspace: total_L * (10 * H + I) * 2 + 32 MiB.
  // Excludes model weights, position/RoPE tables and processor buffers.
  uint64_t workspace_bytes = 0;
};

// Shared CPU source/geometry validation. Does not admit a device execution or
// context: callers must also run PlanMedia before requesting GPU buffers.
MediaPlanResult PlanMediaSources(std::span<const MediaPlanInput> inputs,
                                 const ProcessorConfig& image_config,
                                 const ProcessorConfig& video_config,
                                 const MediaPlanLimits& limits,
                                 MediaPlan* out) noexcept;

// Validate EVERY header and budget before any caller decodes an item. On any
// failure, *out is empty (never a usable partial plan). Inputs are borrowed
// only during this call; the returned plan contains no borrowed references.
MediaPlanResult PlanMedia(std::span<const MediaPlanInput> inputs,
                          const ProcessorConfig& image_config,
                          const ProcessorConfig& video_config,
                          const MediaPlanLimits& limits,
                          const MediaContextBudget& context,
                          const MediaDeviceBudget& device,
                          MediaPlan* out) noexcept;

}  // namespace q4t::vision
