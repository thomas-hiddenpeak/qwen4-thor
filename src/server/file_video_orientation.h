#pragma once

#include <algorithm>
#include <array>
#include <bit>
#include <cstdint>
#include <optional>
#include <stdexcept>
#include <string_view>
#include <vector>

#include "file_video_region.h"

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

using TrackSize = std::array<uint32_t, 2>;  // Unsigned 16.16 display extents.
inline bool FractionalTrack(const TrackSize& size) {
  return ((size[0] | size[1]) & 0xffff) != 0;
}
struct DisplayClips {
  std::optional<ClipRect> movie, track;
  TrackSize track_size{};
  std::optional<QuickDrawRegion> movie_region{}, track_region{};
  bool Complex() const { return movie_region || track_region; }
  bool Any() const { return movie.has_value() || track.has_value(); }
  bool operator==(const DisplayClips&) const = default;
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

// A bounded metadata reader for QuickTime video tracks. Older installed
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
    const auto selected = SelectTrack(bytes, false);
    if (!selected) return std::nullopt;
    return selected->matrices;
  }

  struct SelectedTrack {
    DisplayMatrices matrices;
    bool has_alternates;
    std::optional<uint64_t> duration_ns;
    DisplayClips clips;
  };
  struct VideoLayer {
    uint32_t track_id;
    int16_t layer;
    uint16_t alternate_group;
    std::string_view track_atom, header;
  };

  // Views borrow the original container bytes. Layers are in paint order;
  // members of a nonzero alternate group are alternatives for playback.
  struct Presentation {
    std::string_view movie, header;
    size_t video_track_count = 0;
    std::vector<VideoLayer> enabled;
  };

  std::optional<Presentation> ReadPresentation(std::string_view bytes) {
    nodes_ = 0;
    if (bytes.size() < 12 || bytes.substr(4, 4) != "ftyp") return std::nullopt;
    const auto movie = Find(bytes, "moov");
    Presentation result{movie, Find(movie, "mvhd"), 0, {}};
    auto remaining = movie;
    std::string_view only_track, only_header;
    while (!remaining.empty()) {
      const auto box = Next(&remaining);
      if (box.type != "trak") continue;
      const auto handler = Find(Find(box.data, "mdia"), "hdlr");
      Require(handler.size() >= 12);
      if (handler.substr(8, 4) != "vide") continue;
      only_track = box.data;
      only_header = Find(only_track, "tkhd");
      Require(only_header.size() >= 4);
      ++result.video_track_count;
      if ((static_cast<unsigned char>(only_header[3]) & 1) != 0)
        result.enabled.push_back(ReadLayer(only_track, only_header));
    }
    // Some older single-track files omit the enabled flag.
    if (result.enabled.empty() && result.video_track_count == 1)
      result.enabled.push_back(ReadLayer(only_track, only_header));
    Require(!result.enabled.empty());
    std::stable_sort(result.enabled.begin(), result.enabled.end(),
                     [](const VideoLayer& lhs, const VideoLayer& rhs) {
                       return lhs.layer > rhs.layer;
                     });
    return result;
  }

  std::optional<SelectedTrack> SelectTrack(std::string_view bytes,
                                           bool read_clips = true) {
    const auto presentation = ReadPresentation(bytes);
    if (!presentation) return std::nullopt;
    Require(presentation->enabled.size() == 1);
    return DescribeLayer(*presentation, presentation->enabled.front(),
                         read_clips);
  }

  SelectedTrack DescribeLayer(const Presentation& presentation,
                              const VideoLayer& layer, bool read_clips = true) {
    const auto selected = layer.track_atom;
    const auto track = layer.header;
    const auto movie = presentation.movie;
    const auto header = presentation.header;
    SelectedTrack result{{Matrix(header, 36, 48), Matrix(track, 40, 52)},
                         presentation.video_track_count > 1, {}, {}};
    if (read_clips) {
      result.clips.movie = Clip(movie, &result.clips.movie_region);
      result.clips.track = Clip(selected, &result.clips.track_region);
      const size_t at = track[0] == 0 ? 76 : 88;
      Require(track.size() >= at + 8);
      for (size_t i = 0; i < 2; ++i)
        result.clips.track_size[i] = uint32_t(Big(track.substr(at + i * 4, 4)));
      const auto& size = result.clips.track_size;
      // A track without declared dimensions falls back to the decoded grid.
      Require((size[0] && size[1]) ||
              (!size[0] && !size[1] && !result.clips.Any()));
    }
    if (result.has_alternates) {
      const auto scale = Big(header.substr(header[0] == 0 ? 12 : 20, 4));
      const auto duration =
          track[0] == 0 ? track.substr(20, 4) : track.substr(28, 8);
      const auto ticks = Big(duration);
      Require(scale != 0);
      // A fragmented movie can defer track duration to the demuxer.
      if (ticks && ticks != (duration.size() == 4 ? UINT32_MAX : UINT64_MAX)) {
        const auto ns =
            static_cast<unsigned __int128>(ticks) * 1000000000 / scale;
        Require(ns > 0 && ns <= UINT64_MAX);
        result.duration_ns = static_cast<uint64_t>(ns);
      }
    }
    return result;
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
  static VideoLayer ReadLayer(std::string_view atom, std::string_view header) {
    Require(!header.empty() && (header[0] == 0 || header[0] == 1));
    const size_t id = header[0] == 0 ? 12 : 20;
    const size_t layer = header[0] == 0 ? 32 : 44;
    Require(header.size() >= layer + 4);
    return {uint32_t(Big(header.substr(id, 4))),
            std::bit_cast<int16_t>(uint16_t(Big(header.substr(layer, 2)))),
            uint16_t(Big(header.substr(layer + 2, 2))), atom, header};
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
  std::optional<std::string_view> Optional(std::string_view bytes,
                                           std::string_view type) {
    std::optional<std::string_view> result;
    while (!bytes.empty()) {
      const auto box = Next(&bytes);
      if (box.type != type) continue;
      Require(!result);
      result = box.data;
    }
    return result;
  }
  std::string_view Find(std::string_view bytes, std::string_view type) {
    const auto result = Optional(bytes, type);
    Require(result.has_value());
    return *result;
  }
  std::optional<ClipRect> Clip(std::string_view parent,
                               std::optional<QuickDrawRegion>* shape) {
    const auto clip = Optional(parent, "clip");
    if (!clip) return std::nullopt;
    const auto region = Find(*clip, "crgn");
    Require(region.size() >= 10 && Big(region.substr(0, 2)) == region.size());
    const auto coord = [&](size_t offset) {
      return std::bit_cast<int16_t>(uint16_t(Big(region.substr(offset, 2))));
    };
    ClipRect rect{coord(2), coord(4), coord(6), coord(8)};
    Require(rect.top <= rect.bottom && rect.left <= rect.right);
    if (region.size() > 10) shape->emplace(region, rect);
    return rect;
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
