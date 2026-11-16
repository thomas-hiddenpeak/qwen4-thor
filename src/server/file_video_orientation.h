#pragma once

#include <array>
#include <cstdint>
#include <optional>
#include <stdexcept>
#include <string_view>

namespace q4t::server::video_detail {
enum class DisplayDirection {
  kIdentity,
  kRight,
  kHalf,
  kLeft,
  kHorizontal,
  kVertical,
  kTranspose,
  kAntiTranspose
};

// A bounded metadata reader for the one selected video track. Older installed
// demuxers can omit reflected track matrices from their orientation tags.
// Pixel decoding and the actual orthogonal transform remain in the worker.
class MovieDisplay {
 public:
  std::optional<DisplayDirection> Read(std::string_view bytes) {
    if (bytes.size() < 12 || bytes.substr(4, 4) != "ftyp") return std::nullopt;
    const auto movie = Find(bytes, "moov");
    const auto global = Matrix(Find(movie, "mvhd"), 36, 48);
    std::optional<Transform> selected;
    auto remaining = movie;
    while (!remaining.empty()) {
      const auto box = Next(&remaining);
      if (box.type != "trak") continue;
      const auto handler = Find(Find(box.data, "mdia"), "hdlr");
      Require(handler.size() >= 12);
      if (handler.substr(8, 4) != "vide") continue;
      Require(!selected);
      const auto track = Matrix(Find(box.data, "tkhd"), 40, 52);
      // Track coordinates are transformed into movie, then display space.
      selected = Transform{global.a * track.a + global.c * track.b,
                           global.b * track.a + global.d * track.b,
                           global.a * track.c + global.c * track.d,
                           global.b * track.c + global.d * track.d};
    }
    Require(selected.has_value());
    constexpr std::array<Transform, 8> directions = {{{1, 0, 0, 1},
                                                      {0, 1, -1, 0},
                                                      {-1, 0, 0, -1},
                                                      {0, -1, 1, 0},
                                                      {-1, 0, 0, 1},
                                                      {1, 0, 0, -1},
                                                      {0, 1, 1, 0},
                                                      {0, -1, -1, 0}}};
    for (size_t i = 0; i < directions.size(); ++i)
      if (*selected == directions[i]) return DisplayDirection(i);
    throw std::runtime_error("invalid orthogonal video display matrix");
  }

 private:
  struct Box {
    std::string_view type, data;
  };
  struct Transform {
    int a, b, c, d;
    bool operator==(const Transform&) const = default;
  };
  size_t nodes_ = 0;
  static void Require(bool condition) {
    if (!condition) throw std::runtime_error("invalid video display metadata");
  }
  static uint64_t Big(std::string_view value) {
    uint64_t result = 0;
    for (unsigned char byte : value) result = (result << 8) | byte;
    return result;
  }
  Box Next(std::string_view* remaining) {
    Require(++nodes_ <= 65536 && remaining->size() >= 8);
    auto size = Big(remaining->substr(0, 4));
    size_t header = 8;
    if (size == 1) {
      Require(remaining->size() >= 16);
      size = Big(remaining->substr(8, 8));
      header = 16;
    } else if (!size) {
      size = remaining->size();
    }
    Require(size >= header && size <= remaining->size());
    const Box result{remaining->substr(4, 4),
                     remaining->substr(header, size - header)};
    remaining->remove_prefix(size);
    return result;
  }
  std::string_view Find(std::string_view bytes, std::string_view type) {
    std::optional<std::string_view> result;
    while (!bytes.empty()) {
      const auto box = Next(&bytes);
      if (box.type != type) continue;
      Require(!result);
      result = box.data;
    }
    Require(result.has_value());
    return *result;
  }
  static Transform Matrix(std::string_view bytes, size_t v0, size_t v1) {
    Require(!bytes.empty() && (bytes[0] == 0 || bytes[0] == 1));
    const auto offset = bytes[0] == 0 ? v0 : v1;
    Require(bytes.size() >= offset + 36);
    const auto cell = [&](size_t index) {
      return Big(bytes.substr(offset + index * 4, 4));
    };
    Require(cell(2) == 0 && cell(5) == 0 && cell(8) == (1U << 30));
    const auto unit = [&](size_t index) {
      const auto value = cell(index);
      if (value == 0) return 0;
      if (value == 65536) return 1;
      Require(value == 0xffff0000U);
      return -1;
    };
    Transform value{unit(0), unit(1), unit(3), unit(4)};
    Require(value.a * value.a + value.c * value.c == 1 &&
            value.b * value.b + value.d * value.d == 1 &&
            value.a * value.b + value.c * value.d == 0);
    return value;
  }
};
}  // namespace q4t::server::video_detail
