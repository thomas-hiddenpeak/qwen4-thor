#pragma once

#include <algorithm>
#include <array>
#include <cstdint>
#include <span>
#include <utility>
#include <vector>

#include "file_video_clipping.h"
#include "file_video_geometry.h"
#include "file_video_integer.h"
#include "file_video_orientation.h"

namespace q4t::server::video_detail {
struct DisplayFraction {
  __int128 numerator = 0, denominator = 1;
};
inline int CompareFraction(const DisplayFraction& a, const DisplayFraction& b) {
  const auto left = DisplayInteger(a.numerator) * DisplayInteger(b.denominator);
  const auto right =
      DisplayInteger(b.numerator) * DisplayInteger(a.denominator);
  return left < right ? -1 : left > right ? 1 : 0;
}
struct ProjectiveGeometry {
  uint32_t width = 0, height = 0;
  DisplayFraction min_x, min_y;
  // Adjugate entries share a determinant that cancels in the homogeneous
  // division. Keep these and pixel-center increments exact, without division.
  std::array<DisplayInteger, 9> inverse;
  std::array<DisplayInteger, 3> first, step_x, step_y;
};

inline Failure MakeProjectiveGeometry(const DisplayMatrices& matrices,
                                      uint32_t width, uint32_t height,
                                      const FileVideoLimits& limits,
                                      ProjectiveGeometry* result) {
  if (!result || !width || !height || limits.max_edge > 8192)
    return Failure::kInvalid;
  if (width > limits.max_edge || height > limits.max_edge ||
      uint64_t(width) * height > limits.max_frame_pixels)
    return Failure::kCapacity;
  std::array<__int128, 9> movie{}, track{}, t{};
  for (size_t i = 0; i < 9; ++i) {
    const int64_t scale = i % 3 == 2 ? 1 : 16384;
    movie[i] = int64_t(matrices.movie[i]) * scale;
    track[i] = int64_t(matrices.track[i]) * scale;
  }
  for (size_t row = 0; row < 3; ++row)
    for (size_t col = 0; col < 3; ++col)
      for (size_t k = 0; k < 3; ++k)
        t[row * 3 + col] += track[row * 3 + k] * movie[k * 3 + col];
  ProjectiveGeometry geometry;
  auto& inverse = geometry.inverse;
  // Cofactor transpose: omitting row c and column r gives inverse[r,c].
  for (size_t row = 0; row < 3; ++row) {
    for (size_t col = 0; col < 3; ++col) {
      const size_t r0 = col == 0 ? 1 : 0, r1 = col == 2 ? 1 : 2;
      const size_t c0 = row == 0 ? 1 : 0, c1 = row == 2 ? 1 : 2;
      auto minor =
          DisplayInteger(t[r0 * 3 + c0]) * DisplayInteger(t[r1 * 3 + c1]) -
          DisplayInteger(t[r0 * 3 + c1]) * DisplayInteger(t[r1 * 3 + c0]);
      inverse[row * 3 + col] = (row + col) % 2 ? -minor : minor;
    }
  }
  const auto determinant = DisplayInteger(t[0]) * inverse[0] +
                           DisplayInteger(t[1]) * inverse[3] +
                           DisplayInteger(t[2]) * inverse[6];
  if (determinant.IsZero()) return Failure::kInvalid;
  std::array<DisplayFraction, 4> xs, ys;
  int denominator_sign = 0;
  for (size_t i = 0; i < 4; ++i) {
    const auto x = (i & 1) ? width : 0;
    const auto y = (i & 2) ? height : 0;
    const auto denominator = t[2] * x + t[5] * y + t[8];
    if (!denominator) return Failure::kCapacity;
    const int sign = denominator < 0 ? -1 : 1;
    if (denominator_sign && sign != denominator_sign)
      return Failure::kCapacity;  // An infinite horizon intersects the source.
    denominator_sign = sign;
    xs[i] = {(t[0] * x + t[3] * y + t[6]) * sign, denominator * sign};
    ys[i] = {(t[1] * x + t[4] * y + t[7]) * sign, denominator * sign};
  }
  const auto extent = [&](const auto& points, DisplayFraction* minimum,
                          uint32_t* pixels) {
    auto low = points[0], high = points[0];
    for (const auto& point : points) {
      if (CompareFraction(point, low) < 0) low = point;
      if (CompareFraction(point, high) > 0) high = point;
    }
    const auto denominator =
        DisplayInteger(low.denominator) * DisplayInteger(high.denominator);
    const auto numerator =
        DisplayInteger(high.numerator) * DisplayInteger(low.denominator) -
        DisplayInteger(low.numerator) * DisplayInteger(high.denominator);
    if (numerator.IsZero()) return Failure::kInvalid;
    if (numerator > denominator * DisplayInteger(limits.max_edge))
      return Failure::kCapacity;
    auto rounded =
        DisplayInteger::FloorRatio(numerator, denominator, limits.max_edge);
    if (DisplayInteger(rounded) * denominator != numerator) ++rounded;
    *minimum = low;
    *pixels = rounded;
    return Failure::kNone;
  };
  const auto x_status = extent(xs, &geometry.min_x, &geometry.width);
  if (x_status != Failure::kNone) return x_status;
  const auto y_status = extent(ys, &geometry.min_y, &geometry.height);
  if (y_status != Failure::kNone) return y_status;
  if (uint64_t(geometry.width) * geometry.height > limits.max_frame_pixels)
    return Failure::kCapacity;
  const auto dx = DisplayInteger(geometry.min_x.denominator);
  const auto dy = DisplayInteger(geometry.min_y.denominator);
  const auto product = dx * dy, common = product * DisplayInteger(2);
  const auto px =
      DisplayInteger(geometry.min_x.numerator) * dy * DisplayInteger(2) +
      product;
  const auto py =
      DisplayInteger(geometry.min_y.numerator) * dx * DisplayInteger(2) +
      product;
  for (size_t i = 0; i < 3; ++i) {
    geometry.first[i] =
        px * inverse[i] + py * inverse[3 + i] + common * inverse[6 + i];
    geometry.step_x[i] = common * inverse[i];
    geometry.step_y[i] = common * inverse[3 + i];
  }
  *result = std::move(geometry);
  return Failure::kNone;
}

inline Failure RenderProjectiveRgb(const DisplayMatrices& matrices,
                                   uint32_t width, uint32_t height,
                                   std::span<const uint8_t> source,
                                   size_t stride, const FileVideoLimits& limits,
                                   ProjectiveGeometry* geometry,
                                   std::vector<uint8_t>* rgb,
                                   const DisplayClips& clips = {}) {
  const uint64_t row_bytes = uint64_t(width) * 3;
  if (!rgb || !geometry || !width || !height || stride < row_bytes ||
      source.size() < row_bytes ||
      height - 1 > (source.size() - row_bytes) / stride)
    return Failure::kInvalid;
  const auto status =
      MakeProjectiveGeometry(matrices, width, height, limits, geometry);
  if (status != Failure::kNone) return status;
  rgb->assign(size_t(geometry->width) * geometry->height * 3, 0);
  auto row = geometry->first;
  for (uint32_t y = 0; y < geometry->height; ++y) {
    auto point = row;
    for (uint32_t x = 0; x < geometry->width; ++x) {
      auto nx = point[0], ny = point[1], denominator = point[2];
      for (size_t i = 0; i < 3; ++i) point[i] = point[i] + geometry->step_x[i];
      if (denominator.IsNegative()) {
        nx = -nx;
        ny = -ny;
        denominator = -denominator;
      }
      if (denominator.IsZero() || nx.IsNegative() || ny.IsNegative() ||
          nx >= denominator * DisplayInteger(width) ||
          ny >= denominator * DisplayInteger(height))
        continue;
      if (clips.Any() && !VisibleAt(clips, matrices.track, nx, ny, denominator))
        continue;
      const auto sample = [&](const DisplayInteger& n, uint32_t size) {
        const int64_t q =
            DisplayInteger::FloorRatio(n * DisplayInteger(65536), denominator,
                                       size * 65536) -
            int64_t{32768};
        const auto low = q >= 0 ? q / 65536 : -((-q + 65535) / 65536);
        return std::array<uint32_t, 3>{
            uint32_t(std::clamp<int64_t>(low, 0, size - 1)),
            uint32_t(std::clamp<int64_t>(low + 1, 0, size - 1)),
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
    for (size_t i = 0; i < 3; ++i) row[i] = row[i] + geometry->step_y[i];
  }
  return Failure::kNone;
}
}  // namespace q4t::server::video_detail
