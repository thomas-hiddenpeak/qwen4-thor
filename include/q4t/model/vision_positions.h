// Host-only 3D positions for actual visual prompt blocks, without CUDA.
#pragma once

#include <algorithm>
#include <array>
#include <cstdint>
#include <limits>
#include <span>
#include <vector>

#include "q4t/status.h"

namespace q4t::model {

// Layout [3,max_len], indexed by logical position. Video callers provide one
// t=1 grid per timestamped temporal group. Coordinates and feature order must
// describe the same blocks. Empty grids mean ordinary text, even if the text
// contains a special token ID. Invalid supplied grids never silently degrade.
inline Status BuildVisionRopePositions(
    std::span<const int32_t> ids, int merge, int image_id, int video_id,
    int max_len, std::span<const std::array<int, 3>> grids,
    std::vector<int>* positions, int* delta) {
  positions->clear();
  *delta = 0;
  const auto invalid = [] {
    return Status::Fail("vision position grid/token contract mismatch");
  };
  if (ids.empty() || max_len <= 0 ||
      max_len > std::numeric_limits<int>::max() / 3 ||
      ids.size() > static_cast<size_t>(max_len) || merge <= 0 ||
      image_id <= 0 || video_id <= 0 || image_id == video_id)
    return invalid();
  // Validate every block before allocating the position table. Products are
  // bounded by the supplied token count before multiplying the final axis.
  size_t block = 0;
  for (size_t p = 0; p < ids.size() && !grids.empty();) {
    const auto id = ids[p];
    if (id != image_id && id != video_id) {
      ++p;
      continue;
    }
    if (block >= grids.size()) return invalid();
    const auto& grid = grids[block++];
    if (grid[0] != 1 || grid[1] <= 0 || grid[2] <= 0 || grid[1] % merge ||
        grid[2] % merge)
      return invalid();
    const auto height = static_cast<size_t>(grid[1] / merge);
    const auto width = static_cast<size_t>(grid[2] / merge);
    if (!height || !width || height > (ids.size() - p) / width)
      return invalid();
    const size_t count = height * width;
    for (size_t k = 0; k < count; ++k)
      if (ids[p + k] != id) return invalid();
    p += count;
  }
  if (block != grids.size()) return invalid();

  std::vector<int> result(static_cast<size_t>(3) * max_len, 0);
  int clock = 0, maximum = -1;
  block = 0;
  for (size_t p = 0; p < ids.size();) {
    if (grids.empty() || (ids[p] != image_id && ids[p] != video_id)) {
      for (int axis = 0; axis < 3; ++axis)
        result[static_cast<size_t>(axis) * max_len + p] = clock;
      maximum = std::max(maximum, clock++);
      ++p;
      continue;
    }
    const auto& grid = grids[block++];
    const int height = grid[1] / merge, width = grid[2] / merge;
    for (int h = 0; h < height; ++h) {
      for (int w = 0; w < width; ++w, ++p) {
        result[p] = clock;
        result[max_len + p] = clock + h;
        result[static_cast<size_t>(2) * max_len + p] = clock + w;
      }
    }
    clock += std::max(height, width);
    maximum = std::max(maximum, clock - 1);
  }
  *delta = maximum + 1 - static_cast<int>(ids.size());
  *positions = std::move(result);
  return Status();
}

}  // namespace q4t::model
