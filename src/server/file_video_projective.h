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
struct MovieCanvas {
  uint32_t width = 0, height = 0;
  DisplayFraction min_x, min_y, max_x, max_y;

  Failure Measure(const FileVideoLimits& limits) {
    const auto pixels = [&](DisplayFraction low, DisplayFraction high,
                            uint32_t* size) {
      if (low.denominator <= 0 || high.denominator <= 0 ||
          limits.max_edge > 8192)
        return Failure::kInvalid;
      const auto denominator =
          DisplayInteger(low.denominator) * DisplayInteger(high.denominator);
      const auto numerator =
          DisplayInteger(high.numerator) * DisplayInteger(low.denominator) -
          DisplayInteger(low.numerator) * DisplayInteger(high.denominator);
      if (numerator.IsZero() || numerator.IsNegative())
        return Failure::kInvalid;
      if (numerator > denominator * DisplayInteger(limits.max_edge))
        return Failure::kCapacity;
      auto rounded =
          DisplayInteger::FloorRatio(numerator, denominator, limits.max_edge);
      if (DisplayInteger(rounded) * denominator != numerator) ++rounded;
      *size = rounded;
      return Failure::kNone;
    };
    uint32_t w, h;
    auto status = pixels(min_x, max_x, &w);
    if (status != Failure::kNone) return status;
    status = pixels(min_y, max_y, &h);
    if (status != Failure::kNone) return status;
    if (uint64_t(w) * h > limits.max_frame_pixels) return Failure::kCapacity;
    width = w;
    height = h;
    return Failure::kNone;
  }

  Failure Include(const MovieCanvas& other, const FileVideoLimits& limits) {
    auto combined = width ? *this : other;
    if (CompareFraction(other.min_x, combined.min_x) < 0)
      combined.min_x = other.min_x;
    if (CompareFraction(other.min_y, combined.min_y) < 0)
      combined.min_y = other.min_y;
    if (CompareFraction(other.max_x, combined.max_x) > 0)
      combined.max_x = other.max_x;
    if (CompareFraction(other.max_y, combined.max_y) > 0)
      combined.max_y = other.max_y;
    const auto status = combined.Measure(limits);
    if (status == Failure::kNone) *this = combined;
    return status;
  }
};
struct ProjectiveGeometry : MovieCanvas {
  // The common determinant cancels when dividing homogeneous coordinates.
  std::array<DisplayInteger, 9> inverse;
};

inline Failure MakeProjectiveGeometry(const DisplayMatrices& matrices,
                                      uint32_t width, uint32_t height,
                                      const FileVideoLimits& limits,
                                      ProjectiveGeometry* result,
                                      TrackSize extent = {}) {
  if (!result || !width || !height || limits.max_edge > 8192)
    return Failure::kInvalid;
  if (width > limits.max_edge || height > limits.max_edge ||
      uint64_t(width) * height > limits.max_frame_pixels)
    return Failure::kCapacity;
  const uint32_t unit = extent[0] ? 65536 : 1;
  if (!extent[0] && !extent[1]) extent = {width, height};
  if (!extent[0] || !extent[1]) return Failure::kInvalid;
  const uint64_t ew = (uint64_t(extent[0]) + unit - 1) / unit;
  const uint64_t eh = (uint64_t(extent[1]) + unit - 1) / unit;
  if (ew > limits.max_edge || eh > limits.max_edge ||
      ew * eh > limits.max_frame_pixels)
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
    const auto x = (i & 1) ? extent[0] : 0;
    const auto y = (i & 2) ? extent[1] : 0;
    const auto denominator = t[2] * x + t[5] * y + t[8] * unit;
    if (!denominator) return Failure::kCapacity;
    const int sign = denominator < 0 ? -1 : 1;
    if (denominator_sign && sign != denominator_sign)
      return Failure::kCapacity;  // An infinite horizon intersects the source.
    denominator_sign = sign;
    xs[i] = {(t[0] * x + t[3] * y + t[6] * unit) * sign, denominator * sign};
    ys[i] = {(t[1] * x + t[4] * y + t[7] * unit) * sign, denominator * sign};
  }
  const auto less = [](DisplayFraction a, DisplayFraction b) {
    return CompareFraction(a, b) < 0;
  };
  const auto [xmin, xmax] = std::minmax_element(xs.begin(), xs.end(), less);
  const auto [ymin, ymax] = std::minmax_element(ys.begin(), ys.end(), less);
  geometry.min_x = *xmin;
  geometry.max_x = *xmax;
  geometry.min_y = *ymin;
  geometry.max_y = *ymax;
  const auto status = geometry.Measure(limits);
  if (status != Failure::kNone) return status;
  *result = std::move(geometry);
  return Failure::kNone;
}

inline Failure RenderProjectiveRgb(const DisplayMatrices& matrices,
                                   uint32_t width, uint32_t height,
                                   std::span<const uint8_t> source,
                                   size_t stride, const FileVideoLimits& limits,
                                   ProjectiveGeometry* geometry,
                                   std::vector<uint8_t>* rgb,
                                   const DisplayClips& clips = {},
                                   const MovieCanvas* canvas = nullptr) {
  const uint64_t row_bytes = uint64_t(width) * 3;
  if (!rgb || !geometry || !width || !height || stride < row_bytes ||
      source.size() < row_bytes ||
      height - 1 > (source.size() - row_bytes) / stride)
    return Failure::kInvalid;
  const bool scaled =
      FractionalTrack(clips.track_size) || (canvas && clips.track_size[0]);
  const auto status =
      MakeProjectiveGeometry(matrices, width, height, limits, geometry,
                             scaled ? clips.track_size : TrackSize{});
  if (status != Failure::kNone) return status;
  const MovieCanvas& target = canvas ? *canvas : *geometry;
  if (canvas) {
    auto measured = *canvas;
    const auto valid = measured.Measure(limits);
    if (valid != Failure::kNone) return valid;
    if (measured.width != target.width || measured.height != target.height ||
        rgb->size() != size_t(target.width) * target.height * 3)
      return Failure::kInvalid;
  } else {
    rgb->assign(size_t(target.width) * target.height * 3, 0);
  }
  const uint32_t unit = scaled ? 65536 : 1;
  const TrackSize extent = scaled ? clips.track_size : TrackSize{width, height};
  const auto& inverse = geometry->inverse;
  std::array<DisplayInteger, 3> row, step_x, step_y;
  const auto dx = DisplayInteger(target.min_x.denominator);
  const auto dy = DisplayInteger(target.min_y.denominator);
  const auto product = dx * dy, common = product * DisplayInteger(2);
  const auto px =
      DisplayInteger(target.min_x.numerator) * dy * DisplayInteger(2) + product;
  const auto py =
      DisplayInteger(target.min_y.numerator) * dx * DisplayInteger(2) + product;
  for (size_t i = 0; i < 3; ++i) {
    row[i] = px * inverse[i] + py * inverse[3 + i] + common * inverse[6 + i];
    step_x[i] = common * inverse[i];
    step_y[i] = common * inverse[3 + i];
  }
  for (uint32_t y = 0; y < target.height; ++y) {
    auto point = row;
    for (uint32_t x = 0; x < target.width; ++x) {
      auto nx = point[0], ny = point[1], denominator = point[2];
      for (size_t i = 0; i < 3; ++i) point[i] = point[i] + step_x[i];
      if (denominator.IsNegative()) {
        nx = -nx;
        ny = -ny;
        denominator = -denominator;
      }
      if (denominator.IsZero() || nx.IsNegative() || ny.IsNegative() ||
          nx * unit >= denominator * extent[0] ||
          ny * unit >= denominator * extent[1])
        continue;
      if (clips.Any() && !VisibleAt(clips, matrices.track, nx, ny, denominator))
        continue;
      const auto sample = [&](const DisplayInteger& n, uint32_t size,
                              uint32_t edge) {
        const int64_t q =
            DisplayInteger::FloorRatio(
                scaled ? n * unit * size * 65536 : n * 65536,
                scaled ? denominator * edge : denominator, size * 65536) -
            int64_t{32768};
        const auto low = q >= 0 ? q / 65536 : -((-q + 65535) / 65536);
        return std::array<uint32_t, 3>{
            uint32_t(std::clamp<int64_t>(low, 0, size - 1)),
            uint32_t(std::clamp<int64_t>(low + 1, 0, size - 1)),
            uint32_t(q - low * 65536)};
      };
      const auto sx = sample(nx, width, extent[0]),
                 sy = sample(ny, height, extent[1]);
      const uint64_t wx[] = {65536 - sx[2], sx[2]};
      const uint64_t wy[] = {65536 - sy[2], sy[2]};
      for (size_t channel = 0; channel < 3; ++channel) {
        uint64_t value = 1ULL << 31;
        for (size_t j = 0; j < 2; ++j)
          for (size_t i = 0; i < 2; ++i)
            value += source[size_t(sy[j]) * stride + sx[i] * 3 + channel] *
                     wx[i] * wy[j];
        (*rgb)[(size_t(y) * target.width + x) * 3 + channel] = value >> 32;
      }
    }
    for (size_t i = 0; i < 3; ++i) row[i] = row[i] + step_y[i];
  }
  return Failure::kNone;
}
}  // namespace q4t::server::video_detail
