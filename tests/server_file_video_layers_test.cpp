#include <filesystem>
#include <fstream>
#include <string>

#include "../src/server/file_video_layers.h"
#include "../src/server/file_video_projective.h"
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
            selected->enabled.size() == 1 &&
            selected->enabled[0].track_id == 1);
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
  const auto track =
      Box("trak", Box("tkhd", header) + Box("mdia", Box("hdlr", handler)));
  const auto bytes = Box("ftyp", "qt  ") +
                     Box("moov", Box("mvhd", std::string(100, '\0')) + track);
  const auto p = MovieDisplay().ReadPresentation(bytes);
  Q4T_CHECK(p && p->video_track_count == 1 && p->enabled.size() == 1);
  const auto& layer = p->enabled[0];
  Q4T_CHECK(layer.track_id == 0xfedcba98U && layer.layer == -32768 &&
            layer.alternate_group == 0xff01 && layer.header == header);
  return true;
}

Q4T_TEST(file_video_shared_canvas_bounds_and_clipped_layer_reveal_background) {
  using namespace q4t::server;
  using namespace q4t::server::video_detail;
  FileVideoLimits limits;
  const std::vector<uint8_t> red = {220, 0, 0, 220, 0, 0};
  const std::vector<uint8_t> blue = {0, 0, 200, 0, 0, 200};
  DisplayMatrices left, right;
  left.track[6] = -65536;
  right.track[6] = 0;
  ProjectiveGeometry a, b;
  Q4T_CHECK(MakeProjectiveGeometry(left, 2, 1, limits, &a) == Failure::kNone);
  Q4T_CHECK(MakeProjectiveGeometry(right, 2, 1, limits, &b) == Failure::kNone);
  MovieCanvas canvas = a;
  Q4T_CHECK(canvas.Include(b, limits) == Failure::kNone);
  Q4T_CHECK(canvas.width == 3 && canvas.height == 1 &&
            CompareFraction(canvas.min_x, {-1, 1}) == 0);
  std::vector<uint8_t> out(9, 0);
  Q4T_CHECK(RenderProjectiveRgb(left, 2, 1, red, 6, limits, &a, &out, {},
                                &canvas) == Failure::kNone);
  DisplayClips crop;
  crop.track = ClipRect{0, 1, 1, 2};
  Q4T_CHECK(RenderProjectiveRgb(right, 2, 1, blue, 6, limits, &b, &out, crop,
                                &canvas) == Failure::kNone);
  const std::vector<uint8_t> expected = {220, 0, 0, 220, 0, 0, 0, 0, 200};
  Q4T_CHECK(out == expected);
  auto small = a;
  limits.max_frame_pixels = 2;
  Q4T_CHECK(small.Include(b, limits) == Failure::kCapacity && small.width == 2);
  Q4T_CHECK(RenderProjectiveRgb(right, 2, 1, blue, 6, limits, &b, &out, crop,
                                &canvas) == Failure::kCapacity &&
            out == expected);
  return true;
}

Q4T_TEST(file_video_shared_canvas_rounds_only_after_union) {
  using namespace q4t::server;
  using namespace q4t::server::video_detail;
  const FileVideoLimits limits;
  DisplayClips clip;
  clip.track_size = {3 * 65536 / 2, 65536};
  DisplayMatrices left, right;
  right.track[6] = 65536 / 2;
  ProjectiveGeometry a, b;
  Q4T_CHECK(MakeProjectiveGeometry(left, 1, 1, limits, &a, clip.track_size) ==
            Failure::kNone);
  Q4T_CHECK(MakeProjectiveGeometry(right, 1, 1, limits, &b, clip.track_size) ==
            Failure::kNone);
  MovieCanvas canvas = a;
  Q4T_CHECK(canvas.Include(b, limits) == Failure::kNone && canvas.width == 2);
  std::vector<uint8_t> out(6, 0);
  const std::vector<uint8_t> one = {1, 2, 3}, two = {4, 5, 6};
  Q4T_CHECK(RenderProjectiveRgb(left, 1, 1, one, 3, limits, &a, &out, clip,
                                &canvas) == Failure::kNone);
  Q4T_CHECK(RenderProjectiveRgb(right, 1, 1, two, 3, limits, &b, &out, clip,
                                &canvas) == Failure::kNone);
  const std::vector<uint8_t> expected = {4, 5, 6, 4, 5, 6};
  Q4T_CHECK(out == expected);
  return true;
}

Q4T_TEST(file_video_shared_time_uses_movie_boundaries_and_track_end) {
  using namespace q4t::server;
  using namespace q4t::server::video_detail;
  const std::array<LayerTimeline, 2> tracks = {{
      {30, {{0, 5}, {5, 5}, {10, 5}, {15, 5}, {20, 5}, {25, 5}}},
      {20, {{0, 5}, {5, 5}, {10, 5}, {15, 5}}},
  }};
  FileVideoLimits limits;
  limits.sample_frames = 4;
  MovieSchedule timeline;
  Q4T_CHECK(ScheduleMovie(tracks, limits, &timeline) == Failure::kNone);
  Q4T_CHECK(timeline.duration == 30 && timeline.presentation_frames == 6 &&
            timeline.samples.size() == 4);
  const size_t expected[] = {0, 2, 4, 5};
  for (size_t i = 0; i < 4; ++i) {
    const auto& frame = timeline.samples[i];
    Q4T_CHECK(frame.index == expected[i] && frame.start == 5 * frame.index &&
              frame.duration == 5 && frame.layer_frames[0] == frame.index);
    Q4T_CHECK(frame.layer_frames[1] ==
              (i < 2 ? std::optional(frame.index) : std::nullopt));
  }
  return true;
}

Q4T_TEST(file_video_shared_time_keeps_gaps_and_bounds_combined_decode) {
  using namespace q4t::server;
  using namespace q4t::server::video_detail;
  std::array<LayerTimeline, 2> tracks = {{
      {12, {{2, 2}, {7, 0}}},
      {10, {{0, 0}, {8, 1}}},
  }};
  FileVideoLimits limits;
  limits.sample_frames = 16;
  MovieSchedule timeline;
  Q4T_CHECK(ScheduleMovie(tracks, limits, &timeline) == Failure::kNone);
  const uint64_t expected[] = {0, 2, 4, 7, 8, 9};
  Q4T_CHECK(timeline.samples.size() == std::size(expected));
  for (size_t i = 0; i < std::size(expected); ++i)
    Q4T_CHECK(timeline.samples[i].start == expected[i]);
  Q4T_CHECK(!timeline.samples[0].layer_frames[0] &&
            !timeline.samples[2].layer_frames[0] &&
            !timeline.samples[5].layer_frames[1]);
  limits.max_scanned_frames = 3;
  Q4T_CHECK(ScheduleMovie(tracks, limits, &timeline) == Failure::kCapacity);
  limits.max_scanned_frames = 4;
  Q4T_CHECK(ScheduleMovie(tracks, limits, &timeline) == Failure::kCapacity);
  limits.max_scanned_frames = 10;
  tracks[0].frames[1].duration = UINT64_MAX;
  Q4T_CHECK(ScheduleMovie(tracks, limits, &timeline) == Failure::kInvalid &&
            timeline.samples.size() == std::size(expected));
  return true;
}
