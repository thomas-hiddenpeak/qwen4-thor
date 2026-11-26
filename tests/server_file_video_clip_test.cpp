#include "q4t/server/file_video.h"

#include <algorithm>
#include <chrono>

#include "../src/server/file_video_clipping.h"
#include "../src/server/object_protocol.h"
#include "q4t/test.h"
#include "stb/stb_image.h"
#include "video_affine_fixture.h"

namespace {
using namespace q4t::server;
using namespace q4t::server::video_detail;
using namespace q4t::server::object_detail;
using namespace std::chrono_literals;

constexpr ClipRect kFull{0, 0, 64, 128};
std::string Original(ClipRect track, ClipRect movie,
                     DisplayMatrices matrices = {}) {
  auto bytes = q4t::test::video_affine::Source(matrices, "rect-clips.mov");
  // The fixture has one track region inside moov/trak and one at moov's end.
  size_t pos = 0;
  for (auto rect : {track, movie}) {
    pos = bytes.find("crgn", pos);
    if (pos == bytes.npos || bytes.at(pos + 5) != 10)
      throw std::runtime_error("expected short QuickDraw region in fixture");
    pos += 6;
    for (auto v : {rect.top, rect.left, rect.bottom, rect.right}) {
      bytes.at(pos++) = char(uint16_t(v) >> 8);
      bytes.at(pos++) = char(uint16_t(v) & 255);
    }
  }
  return bytes;
}
ObjectReply Decode(const std::string& source, FileVideoPixels* pixels) {
  return DecodeVideoContainer(
      source, {}, {"client:clip", std::chrono::steady_clock::now() + 15s, {}},
      pixels);
}
std::vector<uint8_t> Rgb(const FileVideoFrame& frame, int width, int height) {
  int w, h, channels;
  auto* bytes =
      stbi_load_from_memory(reinterpret_cast<const uint8_t*>(frame.png.data()),
                            frame.png.size(), &w, &h, &channels, 3);
  if (!bytes) return {};
  std::vector<uint8_t> result;
  if (w == width && h == height)
    result.assign(bytes, bytes + size_t(w) * h * 3);
  stbi_image_free(bytes);
  return result;
}
}  // namespace

Q4T_TEST(file_video_rect_clips_preserve_only_the_intersection) {
  DisplayMatrices transforms;
  transforms.track[6] = 32 * 65536;
  transforms.movie[7] = -16 * 65536;
  auto plain = Original(kFull, kFull, transforms);
  for (size_t p = 0; (p = plain.find("clip", p)) != plain.npos; p += 4)
    plain.replace(p, 4, "free");
  const auto source = Original({1, 4, 40, 100}, {2, 40, 30, 80}, transforms);
  FileVideoPixels before, after;
  Q4T_CHECK(Decode(plain, &before).ok() && Decode(source, &after).ok());
  Q4T_CHECK(before.width == 128 && before.height == 64 &&
            after.width == before.width && after.height == before.height &&
            after.frames.size() == before.frames.size() &&
            after.frames.size() >= 2);
  Q4T_CHECK(after.conversion.GetString("format") == "q4t.cpu_video_png.v6");
  for (size_t i = 0; i < after.frames.size(); ++i) {
    const auto& a = before.frames[i];
    const auto& b = after.frames[i];
    Q4T_CHECK(a.source_index == b.source_index &&
              a.timestamp_ns == b.timestamp_ns &&
              a.duration_ns == b.duration_ns);
    auto reference = Rgb(a, 128, 64), actual = Rgb(b, 128, 64);
    Q4T_CHECK(reference.size() == 128 * 64 * 3 &&
              actual.size() == reference.size());
    for (int y = 0; y < 64; ++y)
      for (int x = 0; x < 128; ++x) {
        const bool show = x >= 8 && x < 48 && y >= 2 && y < 30;
        const auto at = (y * 128 + x) * 3;
        for (int c = 0; c < 3; ++c)
          Q4T_CHECK(actual[at + c] == (show ? reference[at + c] : 0));
      }
  }
  return true;
}

Q4T_TEST(file_video_rect_clip_replay_binds_signed_source_geometry) {
  const auto source = Original({-3, -10, 32, 64}, {2, 8, 30, 48});
  FileVideoPixels output;
  Q4T_CHECK(Decode(source, &output).ok());
  auto& display = Field(output.conversion, "display_geometry");
  auto& raw = Field(display, "track_clip");
  Q4T_CHECK(raw.array.size() == 4 && raw.array[0].number == -3);
  const auto digest = output.conversion.GetString("source_sha256");
  Q4T_CHECK(ValidateVideoSnapshot(digest, {}, output).ok());
  raw.array[0] = Number(-4);
  Q4T_CHECK(ValidateVideoPixels(source, {}, output).http_status == 400);
  raw.array[0] = Number(40000);
  Q4T_CHECK(ValidateVideoSnapshot(digest, {}, output).http_status == 400);
  raw.array[0] = Number(36);
  Q4T_CHECK(ValidateVideoSnapshot(digest, {}, output).http_status == 400);
  raw.array.clear();
  Field(display, "movie_clip").array.clear();
  Q4T_CHECK(ValidateVideoSnapshot(digest, {}, output).http_status == 400);
  return true;
}

Q4T_TEST(file_video_rect_clips_projective_coordinates_respect_signed_w) {
  DisplayClips clips{ClipRect{0, 1, 3, 2}, ClipRect{0, 0, 8, 8}};
  auto track = kIdentityMatrix;
  // x' = 2x/(x+2), y' = 2y/(x+2).
  track[2] = 1 << 29;
  Q4T_CHECK(!VisibleAt(clips, track, 3, 1, 2));
  Q4T_CHECK(VisibleAt(clips, track, 2, 1, 1));
  for (auto& coefficient : track) coefficient = -coefficient;
  Q4T_CHECK(VisibleAt(clips, track, 2, 1, 1));
  Q4T_CHECK(!VisibleAt(clips, track, 2, 8, 1));
  track[2] = 1 << 29;
  Q4T_CHECK(!VisibleAt(clips, track, 2, 1, 1));
  return true;
}

Q4T_TEST(file_video_rect_clips_reject_ambiguous_or_invalid_geometry) {
  auto invalid = Original({32, 64, 0, 0}, kFull);
  FileVideoPixels pixels;
  Q4T_CHECK(Decode(invalid, &pixels).http_status == 400 &&
            pixels.frames.empty());
  auto unknown = Original(kFull, kFull);
  unknown.replace(unknown.find("crgn"), 4, "free");
  Q4T_CHECK(Decode(unknown, &pixels).http_status == 400 &&
            pixels.frames.empty());
  auto long_region = Original(kFull, kFull);
  long_region.at(long_region.find("crgn") + 5) = 12;
  Q4T_CHECK(Decode(long_region, &pixels).http_status == 400 &&
            pixels.frames.empty());
  return true;
}

Q4T_TEST(file_video_rect_clip_tracks_use_the_declared_grid_and_pixel_budget) {
  auto source = Original({0, 0, 40, 192}, {0, 0, 40, 256});
  const auto size = source.find("tkhd") + 4 + 76;
  const auto dimensions = [&](uint32_t w, uint32_t h) {
    auto pos = size;
    for (auto n : {w, h})
      for (int shift : {24, 16, 8, 0}) source.at(pos++) = char(n >> shift);
  };
  dimensions(256 << 16, 40 << 16);
  FileVideoPixels output;
  Q4T_CHECK(Decode(source, &output).ok() && output.width == 256 &&
            output.height == 40);
  for (const auto& frame : output.frames) {
    const auto rgb = Rgb(frame, 256, 40);
    Q4T_CHECK(rgb.size() == 256 * 40 * 3);
    const auto channel = [&](size_t x, size_t y, size_t c) {
      return rgb[(y * 256 + x) * 3 + c];
    };
    Q4T_CHECK(channel(4, 2, 0) > 230 && channel(180, 5, 1) > 230);
    Q4T_CHECK(channel(4, 35, 2) > 230 && channel(180, 35, 0) > 230);
    Q4T_CHECK(channel(191, 5, 1) > 230 && !channel(192, 5, 1));
  }
  dimensions(16384u << 16, 40 << 16);
  Q4T_CHECK(Decode(source, &output).http_status == 413 &&
            output.frames.empty());
  dimensions((256 << 16) + 1, 40 << 16);
  Q4T_CHECK(Decode(source, &output).ok() && output.width == 257 &&
            output.height == 40);
  dimensions(32 << 16, 32 << 16);
  FileVideoLimits limits;
  limits.max_decoded_pixels = 2 * 128 * 64;
  Q4T_CHECK(DecodeVideoContainer(
                source, limits,
                {"client:clip", std::chrono::steady_clock::now() + 15s, {}},
                &output)
                .http_status == 413);
  return true;
}

Q4T_TEST(file_video_region_decodes_diagonal_quadrants_and_binds_source) {
  const auto original = q4t::test::video_affine::Source({}, "region-clips.mov");
  auto unclipped = original;
  unclipped.replace(unclipped.rfind("clip"), 4, "free");
  FileVideoPixels before, after;
  Q4T_CHECK(Decode(unclipped, &before).ok() && Decode(original, &after).ok());
  Q4T_CHECK(after.width == 128 && after.height == 64 &&
            before.frames.size() == after.frames.size() &&
            after.frames.size() >= 2);
  Q4T_CHECK(after.conversion.GetString("format") == "q4t.cpu_video_png.v7");
  for (size_t i = 0; i < after.frames.size(); ++i) {
    Q4T_CHECK(before.frames[i].timestamp_ns == after.frames[i].timestamp_ns &&
              before.frames[i].duration_ns == after.frames[i].duration_ns);
    auto expected = Rgb(before.frames[i], 128, 64);
    Q4T_CHECK(expected.size() == 128 * 64 * 3);
    for (int y = 0; y < 64; ++y)
      for (int x = 0; x < 128; ++x)
        if ((y < 32) != (x < 64))
          std::fill_n(expected.begin() + 3 * (128 * y + x), 3, 0);
    Q4T_CHECK(Rgb(after.frames[i], 128, 64) == expected);
  }
  auto digest = after.conversion.GetString("source_sha256");
  auto& display = Field(after.conversion, "display_geometry");
  Q4T_CHECK(ValidateVideoPixels(original, {}, after).ok());
  Q4T_CHECK(display.GetString("movie_region_sha256").empty());
  auto& retained = Field(display, "track_region_sha256");
  Q4T_CHECK(retained.str.size() == 64);
  retained = String(std::string(64, 'f'));
  Q4T_CHECK(ValidateVideoPixels(original, {}, after).http_status == 400);
  retained = String("");
  Q4T_CHECK(ValidateVideoSnapshot(digest, {}, after).http_status == 400);
  return true;
}

namespace {
std::string RegionWords(std::initializer_list<int16_t> words) {
  std::string bytes(10, '\0');
  for (auto value : words) {
    bytes.push_back(char(uint16_t(value) >> 8));
    bytes.push_back(char(uint16_t(value) & 255));
  }
  return bytes;
}
}  // namespace

Q4T_TEST(file_video_region_hole_and_negative_projective_coordinates) {
  constexpr int16_t end = INT16_MAX;
  const ClipRect bounds{-3, -4, 4, 5};
  const auto bytes = RegionWords(
      {-3, -4, 5, end, -1, -2, 2, end, 2, -2, 2, end, 4, -4, 5, end, end});
  DisplayClips clips;
  clips.movie = bounds;
  clips.movie_region.emplace(bytes, bounds);
  auto track = kIdentityMatrix;
  track[6] = -2 * 65536;
  for (int y = -5; y <= 5; ++y)
    for (int x = -6; x <= 6; ++x) {
      const bool expected = x >= -4 && x < 5 && y >= -3 && y < 4 &&
                            !(x >= -2 && x < 2 && y >= -1 && y < 2);
      for (int sign : {1, -1}) {
        auto transform = track;
        for (auto& value : transform) value *= sign;
        Q4T_CHECK(VisibleAt(clips, transform, 2 * (x + 2) + 1, 2 * y + 1, 2) ==
                  expected);
      }
    }
  return true;
}

Q4T_TEST(file_video_region_rejects_incomplete_or_inconsistent_inversion_rows) {
  constexpr int16_t e = INT16_MAX;
  const auto original = q4t::test::video_affine::Source({}, "region-clips.mov");
  // This fixture has a trailing clip/crgn in the selected track. Keep all atom
  // lengths intact so each counterexample reaches the region decoder.
  const size_t clip = original.rfind("clip");
  Q4T_CHECK(clip != original.npos && original.substr(clip + 8, 4) == "crgn");
  const size_t at = clip + 12;
  Q4T_CHECK(original.size() >= at + 36 && uint8_t(original[at + 1]) == 36);
  for (auto [offset, value] :
       std::vector<std::pair<size_t, int16_t>>{{0, 34},
                                               {2, -1},
                                               {4, -1},
                                               {8, 127},
                                               {12, 64},
                                               {14, 129},
                                               {16, 12},
                                               {18, 0},
                                               {28, 1},
                                               {32, 10},
                                               {34, 2}}) {
    auto bad = original;
    bad[at + offset] = char(uint16_t(value) >> 8);
    bad[at + offset + 1] = char(uint16_t(value) & 255);
    bool rejected = false;
    try {
      MovieDisplay().SelectTrack(bad);
    } catch (const std::runtime_error&) {
      rejected = true;
    }
    Q4T_CHECK(rejected);
  }
  // Two legal scanlines with even point counts can still leave edges open.
  bool rejected = false;
  try {
    QuickDrawRegion open(RegionWords({0, 0, 2, e, 4, 0, 3, e, e}),
                         {0, 0, 4, 3});
  } catch (const std::runtime_error&) {
    rejected = true;
  }
  Q4T_CHECK(rejected);
  return true;
}

Q4T_TEST(file_video_region_large_encoding_preserves_sparse_pixel_membership) {
  // Two rows of 15,000 distinct inversions exceed the signed 16-bit length
  // boundary. The second row closes the region without a full-size bitmap.
  std::string bytes(10, '\0');
  const auto word = [&](int16_t value) {
    bytes.push_back(char(uint16_t(value) >> 8));
    bytes.push_back(char(uint16_t(value) & 255));
  };
  for (int16_t y : {-12, 17}) {
    word(y);
    for (int x = -7000; x < 8000; ++x) word(x);
    word(INT16_MAX);
  }
  word(INT16_MAX);
  const ClipRect bounds{-12, -7000, 17, 7999};
  const uint16_t header[] = {uint16_t(bytes.size()), uint16_t(bounds.top),
                             uint16_t(bounds.left), uint16_t(bounds.bottom),
                             uint16_t(bounds.right)};
  for (size_t i = 0; i < 5; ++i) {
    bytes[2 * i] = char(header[i] >> 8);
    bytes[2 * i + 1] = char(header[i] & 255);
  }
  QuickDrawRegion region(bytes, bounds);
  for (int x = -7001; x <= 8000; ++x) {
    Q4T_CHECK(region.Contains(x, 0) == (x >= -7000 && x < 7999 && x % 2 == 0));
    Q4T_CHECK(!region.Contains(x, -13) && !region.Contains(x, 17));
  }
  return true;
}

Q4T_TEST(file_video_declared_track_grid_matches_full_clip_on_actual_frames) {
  for (const uint32_t fraction : {0U, 16384U, 32768U, 49152U}) {
    auto clipped = Original(kFull, kFull);
    const uint32_t width = (96U << 16) + fraction;
    auto at = clipped.find("tkhd") + 4 + 76;
    for (auto extent : {width, 48U << 16})
      for (int shift : {24, 16, 8, 0}) clipped.at(at++) = char(extent >> shift);
    auto plain = clipped;
    for (size_t p = 0; (p = plain.find("clip", p)) != plain.npos; p += 4)
      plain.replace(p, 4, "free");
    FileVideoPixels a, b;
    Q4T_CHECK(Decode(plain, &a).ok() && Decode(clipped, &b).ok());
    const uint32_t w = fraction == 0 ? 96 : 97;
    Q4T_CHECK(a.width == w && b.width == w && a.height == 48 && b.height == 48);
    Q4T_CHECK(a.conversion.GetString("format") == "q4t.cpu_video_png.v8");
    Q4T_CHECK(b.conversion.GetString("format") ==
              (fraction ? "q4t.cpu_video_png.v8" : "q4t.cpu_video_png.v6"));
    Q4T_CHECK(a.frames.size() >= 2 && a.frames.size() == b.frames.size());
    for (size_t i = 0; i < a.frames.size(); ++i) {
      const auto rgb = Rgb(a.frames[i], w, 48);
      Q4T_CHECK(rgb.size() == w * 48 * 3 && rgb == Rgb(b.frames[i], w, 48));
      Q4T_CHECK(a.frames[i].source_index == b.frames[i].source_index &&
                a.frames[i].timestamp_ns == b.frames[i].timestamp_ns &&
                a.frames[i].duration_ns == b.frames[i].duration_ns);
      if (fraction && fraction <= 32768U) {
        for (size_t y = 0; y < 48; ++y) {
          const auto edge = rgb.begin() + (y * w + 96) * 3;
          Q4T_CHECK(std::all_of(edge, edge + 3, [](auto c) { return c == 0; }));
        }
      }
    }
    Q4T_CHECK(
        ValidateVideoSnapshot(a.conversion.GetString("source_sha256"), {}, a)
            .ok());
    auto& fixed =
        Field(Field(a.conversion, "display_geometry"), "track_size_fixed16");
    Q4T_CHECK(fixed.array.size() == 2 && fixed.array[0].number == width);
    fixed.array[0] = Number(width + 1U);
    Q4T_CHECK(ValidateVideoPixels(plain, {}, a).http_status == 400);
  }
  return true;
}
