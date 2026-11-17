#include "q4t/server/file_video.h"

#include <cmath>
#include <vector>

#include "../src/server/file_video_affine.h"
#include "../src/server/object_protocol.h"
#include "q4t/server/file_document_worker.h"
#include "q4t/server/lifetime.h"
#include "q4t/test.h"
#include "stb/stb_image.h"
#include "video_affine_fixture.h"

namespace {
using namespace q4t::server;
using namespace q4t::server::object_detail;
using namespace q4t::server::video_detail;
using namespace q4t::test::video_affine;
using namespace std::chrono_literals;
BinaryAccess Access() {
  return {"client:video-affine", std::chrono::steady_clock::now() + 15s, {}};
}
bool Ok(const ObjectReply& reply) {
  if (!reply.ok())
    std::fprintf(stderr, "%s: %s\n", reply.code.c_str(), reply.message.c_str());
  return reply.ok();
}
bool Decode(const std::string& source, FileVideoPixels* output) {
  GrammarControl control;
  control.counters = std::make_shared<GrammarWorkerCounters>();
  Q4T_CHECK(Ok(DecodeVideoContainer(source, {}, Access(), output, control)));
  detail::PollRequestWorkers();
  Q4T_CHECK(control.counters->started == 1 && control.counters->reaped == 1 &&
            control.counters->active == 0);
  Q4T_CHECK(output->frames.size() == 4 && output->scanned_frames == 4 &&
            output->duration_ns == 2000000000 &&
            output->conversion.GetString("format") == "q4t.cpu_video_png.v4" &&
            Ok(ValidateVideoPixels(source, {}, *output)));
  for (size_t i = 0; i < output->frames.size(); ++i)
    Q4T_CHECK(output->frames[i].source_index == i &&
              output->frames[i].timestamp_ns == i * 500000000ULL &&
              output->frames[i].duration_ns == 500000000);
  return true;
}
struct Point {
  int x, y, color;
};
bool Colors(const FileVideoPixels& video, std::initializer_list<Point> points) {
  const int colors[][3] = {
      {255, 0, 0}, {0, 255, 0}, {0, 0, 255}, {255, 255, 0}, {0, 0, 0}};
  for (const auto& frame : video.frames) {
    int width, height, channels;
    auto* rgb = stbi_load_from_memory(
        reinterpret_cast<const uint8_t*>(frame.png.data()), frame.png.size(),
        &width, &height, &channels, 3);
    Q4T_CHECK(rgb);
    ScopeExit free([&] { stbi_image_free(rgb); });
    Q4T_CHECK(width == int(video.width) && height == int(video.height));
    for (const auto& p : points) {
      Q4T_CHECK(p.x >= 0 && p.x < width && p.y >= 0 && p.y < height);
      for (int c = 0; c < 3; ++c) {
        const auto actual = rgb[(p.y * width + p.x) * 3 + c];
        if (std::abs(int(actual) - colors[p.color][c]) > 10)
          std::fprintf(stderr, "(%d,%d) channel%d actual%d expected%d\n", p.x,
                       p.y, c, int(actual), colors[p.color][c]);
        Q4T_CHECK(std::abs(int(actual) - colors[p.color][c]) <= 10);
      }
    }
  }
  return true;
}
}  // namespace

Q4T_TEST(file_video_affine_matrix_signed_composition_and_v1_headers) {
  auto matrices = Scale();
  matrices.track[3] = 16384;
  matrices.track[6] = -17 * 65536;
  matrices.movie = {0, 65536, 0, -65536, 0, 0, 5 * 65536, 0, 1 << 30};
  auto source = Source(matrices);
  Q4T_CHECK(MovieDisplay().ReadMatrices(source) == matrices);
  const auto t = matrices.Compose();
  Q4T_CHECK(t.a == 0 && t.b == (1LL << 31) && t.c == -(1LL << 31) &&
            t.d == (1LL << 30) && t.x == (5LL << 32) &&
            t.y == -(__int128(17) << 32));
  // A compact synthetic v1 movie, with known atom sizes and no codec payload.
  const auto box = [](std::string_view name, const std::string& payload) {
    const uint32_t size = payload.size() + 8;
    std::string value;
    for (int shift : {24, 16, 8, 0}) value += char(size >> shift);
    return value + std::string(name) + payload;
  };
  const auto header = [&](std::string_view type, size_t offset) {
    const auto at = source.find(type) + 4;
    auto value = source.substr(at, offset + 36);
    value.insert(4, 12, '\0');
    value[0] = 1;
    return box(type, value);
  };
  std::string handler(12, '\0');
  handler.replace(8, 4, "vide");
  const auto v1 =
      box("ftyp", "isom") +
      box("moov", header("mvhd", 36) +
                      box("trak", header("tkhd", 40) +
                                      box("mdia", box("hdlr", handler))));
  Q4T_CHECK(MovieDisplay().ReadMatrices(v1) == matrices);
  matrices.movie.fill(INT32_MIN);
  matrices.track.fill(INT32_MIN);
  Q4T_CHECK(matrices.Compose().a == (__int128(1) << 63));
  return true;
}

Q4T_TEST(file_video_affine_geometry_checks_singular_overflow_and_exact_bounds) {
  AffineGeometry geometry;
  auto m = Scale();
  Q4T_CHECK(MakeAffineGeometry(m, 128, 64, {}, &geometry) == Failure::kNone &&
            geometry.width == 64 && geometry.height == 32);
  m.track[3] = 32768;
  Q4T_CHECK(MakeAffineGeometry(m, 128, 64, {}, &geometry) == Failure::kNone &&
            geometry.width == 96 && geometry.height == 32);
  m.track[1] = 32768;
  Q4T_CHECK(MakeAffineGeometry(m, 128, 64, {}, &geometry) == Failure::kInvalid);
  m = Scale(INT32_MAX);
  m.movie = m.track;
  Q4T_CHECK(MakeAffineGeometry(m, 8192, 8192, {}, &geometry) ==
            Failure::kCapacity);
  m = Scale(1);
  Q4T_CHECK(MakeAffineGeometry(m, 128, 64, {}, &geometry) == Failure::kNone &&
            geometry.width == 1 && geometry.height == 1);
  return true;
}

Q4T_TEST(file_video_affine_integer_bilinear_center_and_reflection_oracle) {
  std::vector<uint8_t> source;
  for (uint8_t value : {0, 64, 128, 192})
    for (int c = 0; c < 3; ++c) source.push_back(value);
  AffineGeometry geometry;
  std::vector<uint8_t> output;
  auto m = Scale(2 * 65536);
  Q4T_CHECK(RenderAffineRgb(m, 2, 2, source, 6, {}, &geometry, &output) ==
            Failure::kNone);
  const uint8_t expected[] = {0,  16,  48,  64,  32,  48,  80,  96,
                              96, 112, 144, 160, 128, 144, 176, 192};
  for (size_t i = 0; i < 16; ++i)
    for (size_t c = 0; c < 3; ++c) Q4T_CHECK(output[i * 3 + c] == expected[i]);
  m.track[0] *= -1;
  Q4T_CHECK(RenderAffineRgb(m, 2, 2, source, 6, {}, &geometry, &output) ==
            Failure::kNone);
  for (size_t y = 0; y < 4; ++y)
    for (size_t x = 0; x < 4; ++x)
      Q4T_CHECK(output[(y * 4 + x) * 3] == expected[y * 4 + 3 - x]);
  return true;
}

Q4T_TEST(file_video_affine_rgb_stride_bounds_and_black_exterior) {
  const std::vector<uint8_t> source = {255, 0, 0, 0,   255, 0,   199,
                                       199, 0, 0, 255, 255, 255, 0};
  DisplayMatrices m;
  AffineGeometry geometry;
  std::vector<uint8_t> output;
  Q4T_CHECK(RenderAffineRgb(m, 2, 2, source, 8, {}, &geometry, &output) ==
                Failure::kNone &&
            output.size() == 12 && output[6] == 0 && output[8] == 255 &&
            output[9] == 255);
  Q4T_CHECK(RenderAffineRgb(m, 2, 2, std::span(source).first(13), 8, {},
                            &geometry, &output) == Failure::kInvalid);
  Q4T_CHECK(RenderAffineRgb(m, 2, 2, source, SIZE_MAX, {}, &geometry,
                            &output) == Failure::kInvalid);
  m.track[3] = 65536;
  Q4T_CHECK(RenderAffineRgb(m, 2, 2, source, 8, {}, &geometry, &output) ==
                Failure::kNone &&
            geometry.width == 4 && geometry.height == 2);
  for (size_t c = 0; c < 3; ++c)
    Q4T_CHECK(output[3 * 3 + c] == 0 && output[4 * 3 + c] == 0);
  Q4T_CHECK(source[6] == 199 && source[7] == 199);
  return true;
}

Q4T_TEST(file_video_affine_actual_scale_preserves_pixels_and_frame_intervals) {
  FileVideoPixels video;
  Q4T_CHECK(Decode(Source(Scale()), &video) && video.width == 64 &&
            video.height == 32);
  return Colors(video, {{16, 8, 0}, {48, 8, 1}, {16, 24, 2}, {48, 24, 3}});
}
Q4T_TEST(file_video_affine_actual_shear_preserves_pixels_and_black_bounds) {
  DisplayMatrices m;
  m.track[3] = 32768;
  FileVideoPixels video;
  Q4T_CHECK(Decode(Source(m), &video) && video.width == 160 &&
            video.height == 64);
  return Colors(video, {{40, 16, 0},
                        {104, 16, 1},
                        {56, 48, 2},
                        {120, 48, 3},
                        {2, 60, 4},
                        {150, 2, 4}});
}
Q4T_TEST(file_video_affine_actual_non_right_angle_rotation_preserves_pixels) {
  DisplayMatrices m;
  m.track = {46341, 46341, 0, -46341, 46341, 0, 0, 0, 1 << 30};
  FileVideoPixels video;
  Q4T_CHECK(Decode(Source(m), &video) && video.width == 136 &&
            video.height == 136);
  return Colors(video, {{56, 34, 0},
                        {102, 79, 1},
                        {34, 56, 2},
                        {79, 102, 3},
                        {2, 2, 4},
                        {130, 130, 4}});
}
Q4T_TEST(file_video_affine_actual_movie_and_pixel_aspect_compose) {
  auto m = Scale();
  m.movie = {0, 65536, 0, -65536, 0, 0, -8 * 65536, 0, 1 << 30};
  FileVideoPixels video;
  Q4T_CHECK(Decode(Source(m, "wide-pixels.mp4"), &video) && video.width == 32 &&
            video.height == 128);
  return Colors(video, {{8, 32, 2}, {24, 32, 0}, {8, 96, 3}, {24, 96, 1}});
}

Q4T_TEST(
    file_video_affine_conversion_rejects_matrix_policy_and_source_changes) {
  const auto bytes = Source(Scale());
  FileVideoPixels video;
  Q4T_CHECK(Decode(bytes, &video));
  for (int mode = 0; mode < 6; ++mode) {
    auto bad = video;
    auto& display = Field(bad.conversion, "display_geometry");
    auto& matrix = Field(display, "track_matrix");
    if (mode == 0) Set(&display, "policy", String("ignored"));
    if (mode == 1) matrix.array.pop_back();
    if (mode == 2) matrix.array[0] = Number(0.5);
    if (mode == 3) matrix.array[2] = Number(1);
    if (mode == 4) matrix.array[6] = Number(2147483648.0);
    if (mode == 5) matrix.array[0] = Number(65536);
    Q4T_CHECK(!ValidateVideoSnapshot(Digest(bytes), {}, bad).ok());
  }
  auto bad = video;
  Field(Field(bad.conversion, "display_geometry"), "track_matrix").array[6] =
      Number(65536);
  Q4T_CHECK(ValidateVideoPixels(bytes, {}, bad).http_status == 400);
  return true;
}

Q4T_TEST(file_video_affine_downscale_does_not_hide_cumulative_decode_work) {
  const auto source = Source(Scale());
  FileVideoPixels video;
  Q4T_CHECK(Decode(source, &video));
  FileVideoLimits limits;
  limits.max_decoded_pixels = 128 * 64 * 4 - 1;
  Q4T_CHECK(ValidateVideoSnapshot(Digest(source), limits, video).http_status ==
            413);
  auto result = DecodeVideoContainer(source, limits, Access(), &video);
  Q4T_CHECK(result.http_status == 413 && video.frames.empty());
  return true;
}

Q4T_TEST(file_video_affine_actual_singular_and_capacity_publish_no_frames) {
  for (bool singular : {true, false}) {
    auto m = Scale(singular ? 65536 : INT32_MAX);
    if (singular) m.track[1] = m.track[3] = 65536;
    FileVideoPixels output;
    GrammarControl control;
    control.counters = std::make_shared<GrammarWorkerCounters>();
    const auto reply =
        DecodeVideoContainer(Source(m), {}, Access(), &output, control);
    detail::PollRequestWorkers();
    Q4T_CHECK(reply.http_status == (singular ? 400 : 413) &&
              output.frames.empty() && control.counters->started == 1 &&
              control.counters->reaped == 1 && control.counters->active == 0);
  }
  return true;
}
