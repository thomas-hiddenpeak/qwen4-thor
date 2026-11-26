#pragma once

#include <cstdint>
#include <limits>

#include "file_video_protocol.h"

namespace q4t::server::video_detail {
struct DisplayGeometry {
  uint32_t coded_width = 0, coded_height = 0;
  uint32_t pixel_aspect_num = 1, pixel_aspect_den = 1;
  uint32_t square_width = 0, square_height = 0;
  bool operator==(const DisplayGeometry&) const = default;
};

// A QuickTime clip lives in the track's declared display space; sample the
// decoded image onto that grid before applying either clip or matrix.
inline Failure UseTrackGrid(uint32_t width, uint32_t height,
                            const FileVideoLimits& limits,
                            DisplayGeometry* geometry) {
  if (!width || !height) return Failure::kInvalid;
  if (width > limits.max_edge || height > limits.max_edge ||
      uint64_t(width) * height > limits.max_frame_pixels)
    return Failure::kCapacity;
  geometry->square_width = width;
  geometry->square_height = height;
  return Failure::kNone;
}

// Preserve the coded resolution, expanding only the non-square display axis.
// Caps fractions are positive signed 32-bit integers. Check before converting
// or allocating; the product stays below 8192 * INT32_MAX.
inline Failure SquarePixelGeometry(int64_t width, int64_t height,
                                   int64_t numerator, int64_t denominator,
                                   const FileVideoLimits& limits,
                                   DisplayGeometry* result) {
  if (width <= 0 || height <= 0 || numerator <= 0 || denominator <= 0 ||
      limits.max_edge > 8192 ||
      numerator > std::numeric_limits<int32_t>::max() ||
      denominator > std::numeric_limits<int32_t>::max())
    return Failure::kInvalid;
  if (uint64_t(width) > limits.max_edge || uint64_t(height) > limits.max_edge ||
      uint64_t(width) * height > limits.max_frame_pixels)
    return Failure::kCapacity;
  uint64_t square_width = width, square_height = height;
  if (numerator >= denominator)
    square_width =
        (uint64_t(width) * numerator + denominator / 2) / denominator;
  else
    square_height =
        (uint64_t(height) * denominator + numerator / 2) / numerator;
  if (square_width > limits.max_edge || square_height > limits.max_edge ||
      square_width * square_height > limits.max_frame_pixels)
    return Failure::kCapacity;
  *result = {uint32_t(width),        uint32_t(height),
             uint32_t(numerator),    uint32_t(denominator),
             uint32_t(square_width), uint32_t(square_height)};
  return Failure::kNone;
}
}  // namespace q4t::server::video_detail
