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
  Q4T_CHECK(Decode(source, &output).http_status == 400 &&
            output.frames.empty());
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
