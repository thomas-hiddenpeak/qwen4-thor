#pragma once

#include <array>
#include <bit>
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

// QuickTime stores a,b,u,c,d,v,x,y,w. Affine entries are signed 16.16;
// u/v/w are signed 2.30. Retain the original values for frozen replay.
using DisplayMatrix = std::array<int32_t, 9>;
inline constexpr DisplayMatrix kIdentityMatrix = {65536, 0, 0, 0,      65536,
                                                  0,     0, 0, 1 << 30};
inline bool IsAffine(const DisplayMatrix& value) {
  return value[2] == 0 && value[5] == 0 && value[8] == (1 << 30);
}
struct DisplayTransform {
  // Coefficients after composition are exact signed 32.32 values. Products
  // of two signed 16.16 matrices can overflow int64_t before cancellation.
  __int128 a, b, c, d, x, y;
};
struct DisplayMatrices {
  DisplayMatrix movie = kIdentityMatrix, track = kIdentityMatrix;
  bool operator==(const DisplayMatrices&) const = default;
  DisplayTransform Compose() const {
    const auto product = [&](size_t m, size_t t) {
      return __int128(movie[m]) * track[t];
    };
    return {product(0, 0) + product(3, 1),
            product(1, 0) + product(4, 1),
            product(0, 3) + product(3, 4),
            product(1, 3) + product(4, 4),
            product(0, 6) + product(3, 7) + __int128(movie[6]) * 65536,
            product(1, 6) + product(4, 7) + __int128(movie[7]) * 65536};
  }
  std::optional<DisplayDirection> Direction() const {
    // Preserve the existing fast path only when both source matrices are
    // unit orthogonal. General transforms carry their raw matrices in v4.
    const auto unit = [](const DisplayMatrix& v) {
      for (auto i : {0, 1, 3, 4})
        if (v[i] != 0 && v[i] != 65536 && v[i] != -65536) return false;
      return int64_t(v[0]) * v[0] + int64_t(v[3]) * v[3] == (1LL << 32) &&
             int64_t(v[1]) * v[1] + int64_t(v[4]) * v[4] == (1LL << 32) &&
             int64_t(v[0]) * v[1] + int64_t(v[3]) * v[4] == 0;
    };
    if (!IsAffine(movie) || !IsAffine(track) || !unit(movie) || !unit(track))
      return std::nullopt;
    const auto value = Compose();
    constexpr std::array<std::array<int, 4>, 8> directions = {{{1, 0, 0, 1},
                                                               {0, 1, -1, 0},
                                                               {-1, 0, 0, -1},
                                                               {0, -1, 1, 0},
                                                               {-1, 0, 0, 1},
                                                               {1, 0, 0, -1},
                                                               {0, 1, 1, 0},
                                                               {0, -1, -1, 0}}};
    constexpr int64_t scale = 1LL << 32;
    for (size_t i = 0; i < directions.size(); ++i) {
      const auto& v = directions[i];
      if (value.a == v[0] * scale && value.b == v[1] * scale &&
          value.c == v[2] * scale && value.d == v[3] * scale)
        return DisplayDirection(i);
    }
    return std::nullopt;
  }
};

// A bounded metadata reader for the one selected video track. Older installed
// demuxers can omit reflected track matrices from their orientation tags.
// Pixel decoding and display transforms remain in the isolated worker.
class MovieDisplay {
 public:
  std::optional<DisplayDirection> Read(std::string_view bytes) {
    const auto matrices = ReadMatrices(bytes);
    if (!matrices) return std::nullopt;
    const auto direction = matrices->Direction();
    Require(direction.has_value());
    return direction;
  }
  std::optional<DisplayMatrices> ReadMatrices(std::string_view bytes) {
    nodes_ = 0;
    if (bytes.size() < 12 || bytes.substr(4, 4) != "ftyp") return std::nullopt;
    const auto movie = Find(bytes, "moov");
    const auto global = Matrix(Find(movie, "mvhd"), 36, 48);
    std::optional<DisplayMatrices> selected;
    auto remaining = movie;
    while (!remaining.empty()) {
      const auto box = Next(&remaining);
      if (box.type != "trak") continue;
      const auto handler = Find(Find(box.data, "mdia"), "hdlr");
      Require(handler.size() >= 12);
      if (handler.substr(8, 4) != "vide") continue;
      Require(!selected);
      const auto track = Matrix(Find(box.data, "tkhd"), 40, 52);
      selected = DisplayMatrices{global, track};
    }
    Require(selected.has_value());
    return selected;
  }

 private:
  struct Box {
    std::string_view type, data;
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
  static DisplayMatrix Matrix(std::string_view bytes, size_t v0, size_t v1) {
    Require(!bytes.empty() && (bytes[0] == 0 || bytes[0] == 1));
    const auto offset = bytes[0] == 0 ? v0 : v1;
    Require(bytes.size() >= offset + 36);
    DisplayMatrix value;
    for (size_t i = 0; i < value.size(); ++i)
      value[i] = std::bit_cast<int32_t>(
          uint32_t(Big(bytes.substr(offset + i * 4, 4))));
    return value;
  }
};
}  // namespace q4t::server::video_detail
