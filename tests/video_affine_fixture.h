#pragma once

#include <filesystem>
#include <fstream>
#include <string>

#include "../src/server/file_video_orientation.h"

namespace q4t::test::video_affine {
inline std::string Source(const server::video_detail::DisplayMatrices& matrices,
                          const char* filename = "geometry.mp4") {
  std::ifstream input(std::filesystem::path(__FILE__).parent_path() /
                          "fixtures/file_video" / filename,
                      std::ios::binary);
  std::string bytes(std::istreambuf_iterator<char>(input), {});
  for (const auto& [name, matrix] : {std::pair{"mvhd", &matrices.movie},
                                     std::pair{"tkhd", &matrices.track}}) {
    const auto offset = bytes.find(name);
    if (offset == bytes.npos || bytes[offset + 4] != 0)
      throw std::runtime_error("expected original v0 video fixture");
    size_t at = offset + 4 + (std::string_view(name) == "mvhd" ? 36 : 40);
    for (const auto cell : *matrix) {
      const auto raw = uint32_t(cell);
      for (int shift : {24, 16, 8, 0}) bytes.at(at++) = char(raw >> shift);
    }
  }
  return bytes;
}
inline server::video_detail::DisplayMatrices Scale(int32_t scale = 32768) {
  server::video_detail::DisplayMatrices value;
  value.track[0] = value.track[4] = scale;
  return value;
}
}  // namespace q4t::test::video_affine
