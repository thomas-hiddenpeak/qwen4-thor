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
  const auto inside = [](const ClipRect& r,
                         const std::optional<QuickDrawRegion>& region,
                         const DisplayInteger& x, const DisplayInteger& y,
                         const DisplayInteger& d) {
    if (x < d * r.left || x >= d * r.right || y < d * r.top ||
        y >= d * r.bottom)
      return false;
    if (!region) return true;
    // The half-open bounds put both exact floors inside signed 16-bit space.
    const auto pixel = [&](const DisplayInteger& n, int lo, int hi) {
      return lo + int(DisplayInteger::FloorRatio(n - d * lo, d, hi - lo - 1));
    };
    return region->Contains(pixel(x, r.left, r.right),
                            pixel(y, r.top, r.bottom));
  };
  if (clips.track &&
      !inside(*clips.track, clips.track_region, x, y, denominator))
    return false;
  if (!clips.movie) return true;
  if (track == kIdentityMatrix)
    return inside(*clips.movie, clips.movie_region, x, y, denominator);
  auto coordinate = [&](size_t column) {
    return x * track[column] + y * track[column + 3] +
           denominator * track[column + 6];
  };
  // Numerator coefficients are 16.16 and the common divisor is 2.30.
  auto mx = coordinate(0) * 16384, my = coordinate(1) * 16384;
  auto md = coordinate(2);
  if (md.IsZero()) return false;
  if (md.IsNegative())
    return inside(*clips.movie, clips.movie_region, -mx, -my, -md);
  return inside(*clips.movie, clips.movie_region, mx, my, md);
}
}  // namespace q4t::server::video_detail
