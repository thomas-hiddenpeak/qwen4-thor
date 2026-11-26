#include <filesystem>
#include <fstream>
#include <string>

#include "../src/server/file_video_orientation.h"
#include "q4t/test.h"

namespace {
using q4t::server::video_detail::MovieDisplay;

std::string Original() {
  std::ifstream file(std::filesystem::path(__FILE__).parent_path() /
                         "fixtures/file_video/alternate-tracks.mp4",
                     std::ios::binary);
  return {std::istreambuf_iterator<char>(file), {}};
}

void Write(std::string* bytes, size_t pos, size_t count, uint32_t value) {
  while (count) {
    bytes->at(pos + --count) = static_cast<char>(value & 0xff);
    value >>= 8;
  }
}

std::string Box(std::string type, const std::string& data) {
  auto box = std::string(4, '\0') + type + data;
  Write(&box, 0, 4, box.size());
  return box;
}
}  // namespace

Q4T_TEST(file_video_presentation_reads_enabled_layers_in_paint_order) {
  auto movie = Original();
  const size_t first = movie.find("tkhd");
  const size_t second = movie.find("tkhd", first + 4);
  Q4T_CHECK(first != movie.npos && second != movie.npos);
  auto selected = MovieDisplay().ReadPresentation(movie);
  Q4T_CHECK(selected && selected->video_track_count == 2 &&
            selected->enabled.size() == 1 && selected->enabled[0].track_id == 1);
  movie[second + 7] |= 1;
  Write(&movie, first + 4 + 32, 2, uint16_t(-2));
  Write(&movie, second + 4 + 32, 2, 3);
  selected = MovieDisplay().ReadPresentation(movie);
  Q4T_CHECK(selected && selected->enabled.size() == 2);
  Q4T_CHECK(selected->enabled[0].track_id == 2 &&
            selected->enabled[0].layer == 3);
  Q4T_CHECK(selected->enabled[1].track_id == 1 &&
            selected->enabled[1].layer == -2);
  Q4T_CHECK(selected->enabled[0].alternate_group == 0 &&
            selected->enabled[1].alternate_group == 0);
  MovieDisplay reader;
  const auto back = reader.DescribeLayer(*selected, selected->enabled[0]);
  const auto front = reader.DescribeLayer(*selected, selected->enabled[1]);
  Q4T_CHECK(back.duration_ns == 2000000000ULL &&
            front.duration_ns == 3000000000ULL);
  Q4T_CHECK(back.clips.track_size[0] == (128u << 16) &&
            front.clips.track_size[0] == (96u << 16));
  Q4T_CHECK(back.matrices.movie == front.matrices.movie &&
            back.matrices.track != front.matrices.track);
  // Reverse only the priority, leaving serialization and track IDs fixed.
  Write(&movie, first + 4 + 32, 2, 4);
  selected = MovieDisplay().ReadPresentation(movie);
  Q4T_CHECK(selected->enabled[0].track_id == 1 &&
            selected->enabled[1].track_id == 2);
  return true;
}

Q4T_TEST(file_video_presentation_v1_layer_and_group_use_wide_header_offsets) {
  std::string header(96, '\0'), handler(12, '\0');
  header[0] = 1;
  Write(&header, 20, 4, 0xfedcba98U);
  Write(&header, 44, 2, 0x8000);
  Write(&header, 46, 2, 0xff01);
  handler.replace(8, 4, "vide");
  const auto track = Box("trak", Box("tkhd", header) +
                                     Box("mdia", Box("hdlr", handler)));
  const auto bytes = Box("ftyp", "qt  ") +
                     Box("moov", Box("mvhd", std::string(100, '\0')) + track);
  const auto p = MovieDisplay().ReadPresentation(bytes);
  Q4T_CHECK(p && p->video_track_count == 1 && p->enabled.size() == 1);
  const auto& layer = p->enabled[0];
  Q4T_CHECK(layer.track_id == 0xfedcba98U && layer.layer == -32768 &&
            layer.alternate_group == 0xff01 && layer.header == header);
  return true;
}
