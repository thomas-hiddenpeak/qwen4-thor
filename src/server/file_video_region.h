#pragma once

#include <algorithm>
#include <bit>
#include <cstdint>
#include <stdexcept>
#include <string>
#include <string_view>
#include <utility>
#include <vector>

namespace q4t::server::video_detail {

struct ClipRect {
  int16_t top, left, bottom, right;
  bool operator==(const ClipRect&) const = default;
};

// A QuickDraw scanline changes the previous row at its inversion points.
// Index the parity of all events with x <= the pixel's x and y <= its y.
// The Fenwick tree stores sorted row coordinates, independent of box area.
class QuickDrawRegion {
 public:
  QuickDrawRegion(std::string_view bytes, ClipRect bounds) : bytes_(bytes) {
    Require(bytes.size() >= 12 && bytes.size() <= UINT16_MAX &&
            bytes.size() % 2 == 0);
    size_t offset = 10;
    const auto word = [&]() {
      Require(offset + 2 <= bytes.size());
      const uint16_t v =
          (uint16_t(uint8_t(bytes[offset])) << 8) | uint8_t(bytes[offset + 1]);
      offset += 2;
      return std::bit_cast<int16_t>(v);
    };
    std::vector<std::pair<int16_t, int16_t>> points;
    int last_y = INT16_MIN - 1;
    for (int16_t y = word(); y != INT16_MAX; y = word()) {
      Require(y > last_y && bounds.top <= y && y <= bounds.bottom);
      last_y = y;
      const size_t start = points.size();
      int last_x = INT16_MIN - 1;
      for (int16_t x = word(); x != INT16_MAX; x = word()) {
        Require(x > last_x && bounds.left <= x && x <= bounds.right);
        last_x = x;
        points.emplace_back(x, y);
        columns_.push_back(x);
      }
      Require((points.size() - start) % 2 == 0);
    }
    Require(offset == bytes.size() && !points.empty() &&
            points.front().second == bounds.top &&
            points.back().second == bounds.bottom);
    std::sort(columns_.begin(), columns_.end());
    columns_.erase(std::unique(columns_.begin(), columns_.end()),
                   columns_.end());
    Require(columns_.front() == bounds.left && columns_.back() == bounds.right);
    changes_.resize(columns_.size() + 1);
    std::vector<bool> active(columns_.size());
    for (auto [x, y] : points) {
      const size_t index =
          std::lower_bound(columns_.begin(), columns_.end(), x) -
          columns_.begin();
      active[index] = !active[index];
      for (size_t i = index + 1; i < changes_.size(); i += i & -i) {
        auto& rows = changes_[i];
        if (!rows.empty() && rows.back() == y)
          rows.pop_back();
        else
          rows.push_back(y);
      }
    }
    Require(
        std::none_of(active.begin(), active.end(), [](bool v) { return v; }));
  }

  bool Contains(int x, int y) const {
    bool inside = false;
    size_t i = std::upper_bound(columns_.begin(), columns_.end(), x) -
               columns_.begin();
    for (; i; i -= i & -i) {
      const auto& rows = changes_[i];
      inside ^=
          (std::upper_bound(rows.begin(), rows.end(), y) - rows.begin()) % 2;
    }
    return inside;
  }

  std::string_view Bytes() const { return bytes_; }
  bool operator==(const QuickDrawRegion& other) const {
    return bytes_ == other.bytes_;
  }

 private:
  static void Require(bool ok) {
    if (!ok) throw std::runtime_error("invalid QuickDraw clipping region");
  }
  std::string bytes_;
  std::vector<int16_t> columns_;
  std::vector<std::vector<int16_t>> changes_;
};
}  // namespace q4t::server::video_detail
