#include "q4t/server/file_video.h"

#include <cmath>
#include <filesystem>
#include <fstream>
#include <limits>
#include <vector>

#include "../src/server/file_video_projective.h"
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
using q4t::test::video_affine::Source;
using namespace std::chrono_literals;
BinaryAccess Access() {
  return {
      "client:video-projective", std::chrono::steady_clock::now() + 15s, {}};
}
DisplayMatrices Perspective() {
  DisplayMatrices value;
  value.track[2] = 1 << 20;  // x' = x / (1 + x/1024).
  return value;
}
DisplayMatrices Homogeneous() {
  DisplayMatrices value;
  value.track[8] = 1 << 29;  // (x,y)/0.5, without changing stored a or d.
  return value;
}
bool Decode(const std::string& source, FileVideoPixels* output) {
  GrammarControl control;
  control.counters = std::make_shared<GrammarWorkerCounters>();
  const auto reply =
      DecodeVideoContainer(source, {}, Access(), output, control);
  if (!reply.ok()) std::fprintf(stderr, "%s\n", reply.code.c_str());
  Q4T_CHECK(reply.ok());
  detail::PollRequestWorkers();
  Q4T_CHECK(control.counters->started == 1 && control.counters->reaped == 1 &&
            control.counters->active == 0);
  Q4T_CHECK(output->frames.size() == 4 && output->scanned_frames == 4 &&
            output->duration_ns == 2000000000 &&
            output->conversion.GetString("format") == "q4t.cpu_video_png.v5" &&
            ValidateVideoPixels(source, {}, *output).ok());
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
    for (const auto& point : points) {
      Q4T_CHECK(point.x >= 0 && point.x < width && point.y >= 0 &&
                point.y < height);
      for (int c = 0; c < 3; ++c)
        Q4T_CHECK(std::abs(int(rgb[(point.y * width + point.x) * 3 + c]) -
                           colors[point.color][c]) <= 10);
    }
  }
  return true;
}
template <class Exception, class Action>
bool Throws(Action action) {
  try {
    action();
  } catch (const Exception&) {
    return true;
  }
  return false;
}
DisplayInteger Power(size_t bit) {
  DisplayInteger value(1);
  for (size_t i = 0; i < bit; ++i) value = value * DisplayInteger(2);
  return value;
}
}  // namespace

Q4T_TEST(file_video_integer_carry_borrow_signed_zero_and_int128_minimum) {
  const auto p = Power(128);
  const auto before = p - DisplayInteger(1);
  Q4T_CHECK(before.Words()[0] == UINT64_MAX &&
            before.Words()[1] == UINT64_MAX && before.Words()[2] == 0 &&
            before + DisplayInteger(1) == p);
  Q4T_CHECK(-before + p == DisplayInteger(1) &&
            before - p == DisplayInteger(-1) &&
            -p + before == DisplayInteger(-1));
  Q4T_CHECK(p + -p == DisplayInteger() && !(-DisplayInteger()).IsNegative());
  const auto minimum = -(__int128(1) << 126) - (__int128(1) << 126);
  Q4T_CHECK(DisplayInteger(minimum) == -Power(127));
  Q4T_CHECK(-p < DisplayInteger(-1) && DisplayInteger(-1) < DisplayInteger() &&
            p > before && -before > -p);
  return true;
}

Q4T_TEST(file_video_integer_multiplication_independent_binomial_words) {
  // (2^128 - 1)^3 = 2^384 - 3*2^256 + 3*2^128 - 1.
  const auto x = Power(128) - DisplayInteger(1);
  const auto cube = x * x * x;
  const std::array<uint64_t, 8> expected = {UINT64_MAX,     UINT64_MAX, 2, 0,
                                            UINT64_MAX - 2, UINT64_MAX, 0, 0};
  Q4T_CHECK(cube.Words() == expected && (-x) * x * x == -cube &&
            (-x) * (-x) * x == cube &&
            cube * DisplayInteger() == DisplayInteger());
  return true;
}

Q4T_TEST(file_video_integer_ratio_corrects_float_hint_at_exact_boundaries) {
  const auto divisor = Power(400) + DisplayInteger(37);
  for (uint32_t q : {1U, 65535U, 536870911U, UINT32_MAX}) {
    const auto product = divisor * DisplayInteger(q);
    Q4T_CHECK(DisplayInteger::FloorRatio(product, divisor, q) == q &&
              DisplayInteger::FloorRatio(product - DisplayInteger(1), divisor,
                                         q) == q - 1 &&
              DisplayInteger::FloorRatio(product + divisor - DisplayInteger(1),
                                         divisor, q) == q);
  }
  Q4T_CHECK(DisplayInteger::FloorRatio(DisplayInteger(), divisor, 0) == 0);
  return true;
}

Q4T_TEST(file_video_integer_overflow_and_invalid_ratio_are_explicit) {
  const auto high = Power(511);
  Q4T_CHECK(
      Throws<std::overflow_error>([&] { (void)(high + high); }) &&
      Throws<std::overflow_error>([&] { (void)(high * DisplayInteger(2)); }) &&
      Throws<std::overflow_error>([&] { (void)(Power(320) * Power(192)); }));
  Q4T_CHECK(
      Throws<std::out_of_range>([] { DisplayInteger::FloorRatio(1, 0, 1); }) &&
      Throws<std::out_of_range>([] { DisplayInteger::FloorRatio(-1, 2, 1); }) &&
      Throws<std::out_of_range>([] { DisplayInteger::FloorRatio(1, -2, 1); }) &&
      Throws<std::out_of_range>([] { DisplayInteger::FloorRatio(4, 2, 1); }));
  return true;
}

Q4T_TEST(file_video_projective_homogeneous_sign_and_exact_center_oracle) {
  std::vector<uint8_t> source;
  for (uint8_t value : {0, 64, 128, 192})
    for (int c = 0; c < 3; ++c) source.push_back(value);
  const uint8_t expected[] = {0,  16,  48,  64,  32,  48,  80,  96,
                              96, 112, 144, 160, 128, 144, 176, 192};
  for (int sign : {1, -1}) {
    auto m = Homogeneous();
    for (auto& entry : m.track) entry *= sign;
    ProjectiveGeometry shape;
    std::vector<uint8_t> output;
    Q4T_CHECK(RenderProjectiveRgb(m, 2, 2, source, 6, {}, &shape, &output) ==
                  Failure::kNone &&
              shape.width == 4 && shape.height == 4);
    for (size_t i = 0; i < 16; ++i)
      for (size_t c = 0; c < 3; ++c)
        Q4T_CHECK(output[i * 3 + c] == expected[i]);
  }
  return true;
}

Q4T_TEST(
    file_video_projective_independent_fraction_geometry_and_pixel_oracles) {
  std::ifstream file(std::filesystem::path(__FILE__).parent_path() /
                     "fixtures/file_video/projective_oracle.json");
  Json oracle;
  Q4T_CHECK(q4t::io::ParseJson(
      std::string(std::istreambuf_iterator<char>(file), {}), &oracle));
  const auto* cases = oracle.GetArray("cases");
  Q4T_CHECK(cases && cases->array.size() == 6);
  for (const auto& row : cases->array) {
    DisplayMatrices matrices;
    for (size_t i = 0; i < 9; ++i) {
      matrices.movie[i] = row.GetArray("movie")->array[i].number;
      matrices.track[i] = row.GetArray("track")->array[i].number;
    }
    std::vector<uint8_t> source, expected, output;
    for (const auto& p : row.GetArray("source")->array)
      source.push_back(p.number);
    for (const auto& p : row.GetArray("output")->array)
      expected.push_back(p.number);
    ProjectiveGeometry shape;
    Q4T_CHECK(RenderProjectiveRgb(matrices, row.GetInt("width"),
                                  row.GetInt("height"), source,
                                  row.GetInt("width") * 3, {}, &shape,
                                  &output) == Failure::kNone);
    Q4T_CHECK(shape.width == row.GetInt("output_width") &&
              shape.height == row.GetInt("output_height") &&
              output == expected);
    for (const auto& [name, fraction] :
         {std::pair{"min_x", shape.min_x}, std::pair{"min_y", shape.min_y}}) {
      const auto& parts = row.GetArray(name)->array;
      const DisplayFraction reference{std::stoll(parts[0].str),
                                      std::stoll(parts[1].str)};
      Q4T_CHECK(CompareFraction(fraction, reference) == 0);
    }
  }
  return true;
}

Q4T_TEST(file_video_projective_track_movie_and_square_pixel_composition) {
  auto m = Perspective();
  m.movie = {0, 65536, 0, -65536, 0, 0, 7 * 65536, -11 * 65536, 1 << 29};
  ProjectiveGeometry shape;
  Q4T_CHECK(MakeProjectiveGeometry(m, 256, 64, {}, &shape) == Failure::kNone &&
            shape.width == 128 && shape.height == 410);
  Q4T_CHECK(CompareFraction(shape.min_x, {-114, 1}) == 0 &&
            CompareFraction(shape.min_y, {-22, 1}) == 0);
  Q4T_CHECK(MovieDisplay().ReadMatrices(Source(m)) == m && !m.Direction());
  return true;
}

Q4T_TEST(file_video_projective_finite_horizon_singular_and_capacity_bounds) {
  auto m = Perspective();
  m.track[2] = -(1 << 23);  // Horizon exactly at x=128.
  ProjectiveGeometry shape;
  Q4T_CHECK(MakeProjectiveGeometry(m, 128, 64, {}, &shape) ==
            Failure::kCapacity);
  m.track[2] -= 1;
  Q4T_CHECK(MakeProjectiveGeometry(m, 128, 64, {}, &shape) ==
            Failure::kCapacity);
  m.track[2] += 2;
  Q4T_CHECK(MakeProjectiveGeometry(m, 128, 64, {}, &shape) ==
            Failure::kCapacity);
  m.track[2] += 3;
  m.track[0] = m.track[4] = 1;
  Q4T_CHECK(MakeProjectiveGeometry(m, 128, 64, {}, &shape) == Failure::kNone &&
            shape.width == 4096 && shape.height == 2048);
  FileVideoLimits limits;
  limits.max_edge = 4095;
  Q4T_CHECK(MakeProjectiveGeometry(m, 128, 64, limits, &shape) ==
            Failure::kCapacity);
  limits = {};
  limits.max_frame_pixels = 4096 * 2048 - 1;
  Q4T_CHECK(MakeProjectiveGeometry(m, 128, 64, limits, &shape) ==
            Failure::kCapacity);
  m.track[0] = 0;
  Q4T_CHECK(MakeProjectiveGeometry(m, 128, 64, {}, &shape) ==
            Failure::kInvalid);
  m.movie.fill(INT32_MIN);
  m.track.fill(INT32_MIN);
  Q4T_CHECK(MakeProjectiveGeometry(m, 128, 64, {}, &shape) ==
            Failure::kInvalid);
  m.movie.fill(INT32_MAX);
  for (size_t i : {0, 4, 8}) --m.movie[i];
  m.track = m.movie;
  Q4T_CHECK(MakeProjectiveGeometry(m, 128, 64, {}, &shape) == Failure::kNone &&
            shape.width == 1 && shape.height == 1);
  return true;
}

Q4T_TEST(file_video_projective_rgb_stride_bounds_and_black_exterior) {
  const std::vector<uint8_t> source = {255, 0, 0, 0,   255, 0,   199,
                                       199, 0, 0, 255, 255, 255, 0};
  auto m = Homogeneous();
  ProjectiveGeometry shape;
  std::vector<uint8_t> output;
  Q4T_CHECK(RenderProjectiveRgb(m, 2, 2, source, 8, {}, &shape, &output) ==
            Failure::kNone);
  Q4T_CHECK(RenderProjectiveRgb(m, 2, 2, std::span(source).first(13), 8, {},
                                &shape, &output) == Failure::kInvalid &&
            RenderProjectiveRgb(m, 2, 2, source, SIZE_MAX, {}, &shape,
                                &output) == Failure::kInvalid);
  m.track[3] = 65536;
  Q4T_CHECK(RenderProjectiveRgb(m, 2, 2, source, 8, {}, &shape, &output) ==
                Failure::kNone &&
            shape.width == 8 && shape.height == 4);
  for (size_t c = 0; c < 3; ++c)
    Q4T_CHECK(output[7 * 3 + c] == 0 && output[24 * 3 + c] == 0);
  return true;
}

Q4T_TEST(file_video_projective_actual_nonunit_w_pixels_and_intervals) {
  for (int sign : {1, -1}) {
    auto m = Homogeneous();
    for (auto& entry : m.track) entry *= sign;
    FileVideoPixels video;
    Q4T_CHECK(
        Decode(Source(m), &video) && video.width == 256 &&
        video.height == 128 &&
        Colors(video, {{64, 32, 0}, {192, 32, 1}, {64, 96, 2}, {192, 96, 3}}));
  }
  return true;
}

Q4T_TEST(file_video_projective_actual_perspective_pixels_and_intervals) {
  FileVideoPixels video;
  Q4T_CHECK(Decode(Source(Perspective()), &video) && video.width == 114 &&
            video.height == 64);
  return Colors(
      video,
      {{31, 16, 0}, {88, 15, 1}, {31, 47, 2}, {88, 44, 3}, {112, 62, 4}});
}

Q4T_TEST(file_video_projective_actual_movie_and_pixel_aspect_compose) {
  auto m = Perspective();
  m.movie = {0, 65536, 0, -65536, 0, 0, 0, 0, 1 << 30};
  FileVideoPixels video;
  Q4T_CHECK(Decode(Source(m, "wide-pixels.mp4"), &video) && video.width == 64 &&
            video.height == 205);
  return Colors(
      video,
      {{49, 60, 0}, {51, 162, 1}, {19, 60, 2}, {24, 162, 3}, {2, 200, 4}});
}

Q4T_TEST(
    file_video_projective_conversion_rejects_source_policy_and_geometry_tampering) {
  const auto bytes = Source(Perspective());
  FileVideoPixels video;
  Q4T_CHECK(Decode(bytes, &video));
  for (int mode = 0; mode < 7; ++mode) {
    auto bad = video;
    auto& display = Field(bad.conversion, "display_geometry");
    auto& matrix = Field(display, "track_matrix");
    if (mode == 0) Set(&display, "policy", String("ignored"));
    if (mode == 1) matrix.array.pop_back();
    if (mode == 2) matrix.array[2] = Number(0.5);
    if (mode == 3) matrix.array[8] = Number(1 << 29);
    if (mode == 4) matrix.array[6] = Number(2147483648.0);
    if (mode == 5)
      Set(&bad.conversion, "format", String("q4t.cpu_video_png.v4"));
    if (mode == 6)
      Set(&Field(bad.conversion, "frames").array[0], "duration_ns", Number(1));
    Q4T_CHECK(!ValidateVideoSnapshot(Digest(bytes), {}, bad).ok());
  }
  auto bad = video;
  Field(Field(bad.conversion, "display_geometry"), "track_matrix").array[6] =
      Number(65536);
  Q4T_CHECK(ValidateVideoPixels(bytes, {}, bad).http_status == 400 &&
            ValidateVideoPixels(Source(Homogeneous()), {}, video).http_status ==
                400);
  return true;
}

Q4T_TEST(file_video_projective_downscale_keeps_cumulative_decode_budget) {
  const auto source = Source(Perspective());
  FileVideoPixels video;
  Q4T_CHECK(Decode(source, &video));
  FileVideoLimits limits;
  limits.max_decoded_pixels = 128 * 64 * 4 - 1;
  Q4T_CHECK(ValidateVideoSnapshot(Digest(source), limits, video).http_status ==
            413);
  Q4T_CHECK(
      DecodeVideoContainer(source, limits, Access(), &video).http_status ==
          413 &&
      video.frames.empty());
  return true;
}

Q4T_TEST(file_video_projective_actual_horizon_singular_and_capacity_cleanup) {
  for (int mode = 0; mode < 3; ++mode) {
    auto m = Perspective();
    if (mode == 0) m.track[2] = -(1 << 23);
    if (mode == 1) m.track[0] = 0;
    if (mode == 2) m.track[8] = 1;
    FileVideoPixels output;
    GrammarControl control;
    control.counters = std::make_shared<GrammarWorkerCounters>();
    const auto reply =
        DecodeVideoContainer(Source(m), {}, Access(), &output, control);
    detail::PollRequestWorkers();
    Q4T_CHECK(reply.http_status == (mode == 1 ? 400 : 413) &&
              output.frames.empty() && control.counters->started == 1 &&
              control.counters->reaped == 1 && control.counters->active == 0);
  }
  return true;
}
