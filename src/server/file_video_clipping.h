#pragma once

#include "file_video_integer.h"
#include "file_video_orientation.h"

namespace q4t::server::video_detail {
// The renderer supplies the exact source pixel center from its inverse map.
// Track clipping precedes the track transform. Movie clipping follows the
// track transform and precedes the movie transform, including translations.
inline bool VisibleAt(const DisplayClips& clips, const DisplayMatrix& track,
                      const DisplayInteger& x, const DisplayInteger& y,
                      const DisplayInteger& denominator) {
  const auto inside = [](const ClipRect& r, const DisplayInteger& x,
                         const DisplayInteger& y, const DisplayInteger& d) {
    return x >= d * r.left && x < d * r.right && y >= d * r.top &&
           y < d * r.bottom;
  };
  if (clips.track && !inside(*clips.track, x, y, denominator)) return false;
  if (!clips.movie) return true;
  if (track == kIdentityMatrix) return inside(*clips.movie, x, y, denominator);
  auto coordinate = [&](size_t column) {
    return x * track[column] + y * track[column + 3] +
           denominator * track[column + 6];
  };
  // Numerator coefficients are 16.16 and the common divisor is 2.30.
  auto mx = coordinate(0) * 16384, my = coordinate(1) * 16384;
  auto md = coordinate(2);
  if (md.IsZero()) return false;
  if (md.IsNegative()) return inside(*clips.movie, -mx, -my, -md);
  return inside(*clips.movie, mx, my, md);
}
}  // namespace q4t::server::video_detail
