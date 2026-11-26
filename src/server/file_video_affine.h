#pragma once

#include <algorithm>
#include <cstddef>
#include <span>
#include <vector>

#include "file_video_clipping.h"
#include "file_video_geometry.h"
#include "file_video_orientation.h"

namespace q4t::server::video_detail {
struct AffineGeometry {
  uint32_t width = 0, height = 0;
  __int128 min_x = 0, min_y = 0;
  DisplayTransform transform{};
};

inline Failure MakeAffineGeometry(const DisplayMatrices& matrices,
                                  uint32_t width, uint32_t height,
                                  const FileVideoLimits& limits,
                                  AffineGeometry* result) {
  if (!result || !width || !height || !IsAffine(matrices.movie) ||
      !IsAffine(matrices.track) || limits.max_edge > 8192)
    return Failure::kInvalid;
  if (width > limits.max_edge || height > limits.max_edge ||
      uint64_t(width) * height > limits.max_frame_pixels)
    return Failure::kCapacity;
  const auto t = matrices.Compose();
  const auto abs = [](__int128 v) { return v < 0 ? -v : v; };
  constexpr int64_t scale = 1LL << 32;
  const auto w = (abs(t.a) * width + abs(t.c) * height + scale - 1) / scale;
  const auto h = (abs(t.b) * width + abs(t.d) * height + scale - 1) / scale;
  if (!w || !h) return Failure::kInvalid;
  if (w > limits.max_edge || h > limits.max_edge ||
      w * h > limits.max_frame_pixels)
    return Failure::kCapacity;
  // Only now are coefficients <= 8192 * 2^32. The determinant, inverse
  // numerators and their 16-bit fractions all fit signed 128-bit arithmetic.
  if (t.a * t.d == t.b * t.c) return Failure::kInvalid;
  *result = {
      uint32_t(w), uint32_t(h),
      std::min<__int128>(t.a * width, 0) + std::min<__int128>(t.c * height, 0),
      std::min<__int128>(t.b * width, 0) + std::min<__int128>(t.d * height, 0),
      t};
  return Failure::kNone;
}

// RGB is mapped from the real frame layout, not inferred from buffer length.
// Validate the entire view before allocation or touching any source pixel.
inline Failure RenderAffineRgb(const DisplayMatrices& matrices, uint32_t width,
                               uint32_t height, std::span<const uint8_t> source,
                               size_t stride, const FileVideoLimits& limits,
                               AffineGeometry* geometry,
                               std::vector<uint8_t>* rgb,
                               const DisplayClips& clips = {}) {
  const uint64_t row_bytes = uint64_t(width) * 3;
  if (!rgb || !geometry || !width || !height || stride < row_bytes ||
      source.size() < row_bytes ||
      height - 1 > (source.size() - row_bytes) / stride)
    return Failure::kInvalid;
  const auto status =
      MakeAffineGeometry(matrices, width, height, limits, geometry);
  if (status != Failure::kNone) return status;
  rgb->assign(size_t(geometry->width) * geometry->height * 3, 0);
  const auto& t = geometry->transform;
  auto determinant = t.a * t.d - t.b * t.c;
  const int sign = determinant < 0 ? -1 : 1;
  determinant *= sign;
  constexpr int64_t unit = 1LL << 32;
  for (uint32_t y = 0; y < geometry->height; ++y) {
    for (uint32_t x = 0; x < geometry->width; ++x) {
      const auto px = geometry->min_x + (int64_t(x) * 2 + 1) * (unit / 2);
      const auto py = geometry->min_y + (int64_t(y) * 2 + 1) * (unit / 2);
      const auto nx = (t.d * px - t.c * py) * sign;
      const auto ny = (t.a * py - t.b * px) * sign;
      if (nx < 0 || ny < 0 || nx >= determinant * width ||
          ny >= determinant * height)
        continue;
      if (clips.Any() && !VisibleAt(clips, matrices.track, nx, ny, determinant))
        continue;
      const auto sample = [&](const __int128 n, uint32_t extent) {
        const auto q = int64_t(n * 65536 / determinant) - 32768;
        // C++ integer division truncates towards zero; floor negative edge
        // coordinates explicitly before clamping neighboring source pixels.
        const auto low = q >= 0 ? q / 65536 : -((-q + 65535) / 65536);
        return std::array<uint32_t, 3>{
            uint32_t(std::clamp<int64_t>(low, 0, extent - 1)),
            uint32_t(std::clamp<int64_t>(low + 1, 0, extent - 1)),
            uint32_t(q - low * 65536)};
      };
      const auto sx = sample(nx, width), sy = sample(ny, height);
      const uint64_t wx[] = {65536 - sx[2], sx[2]};
      const uint64_t wy[] = {65536 - sy[2], sy[2]};
      for (size_t channel = 0; channel < 3; ++channel) {
        uint64_t value = 1ULL << 31;
        for (size_t j = 0; j < 2; ++j)
          for (size_t i = 0; i < 2; ++i)
            value += source[size_t(sy[j]) * stride + sx[i] * 3 + channel] *
                     wx[i] * wy[j];
        (*rgb)[(size_t(y) * geometry->width + x) * 3 + channel] = value >> 32;
      }
    }
  }
  return Failure::kNone;
}
}  // namespace q4t::server::video_detail
