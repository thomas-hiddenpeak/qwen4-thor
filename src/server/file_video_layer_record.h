#pragma once

#include "file_video_orientation.h"
#include "object_protocol.h"

namespace q4t::server::video_detail {
// Bind frozen layer geometry and the original clipping, edit and sample tables
// to the same source track. RGB and presentation timestamps are bound per
// frame.
inline io::Json LayerSource(const MovieDisplay::VideoLayer& layer,
                            const MovieDisplay::SelectedTrack& selected) {
  using namespace object_detail;
  auto matrix = [](const DisplayMatrix& cells) {
    auto array = Array();
    for (auto cell : cells) array.array.push_back(Number(cell));
    return array;
  };
  auto clip = [](const std::optional<ClipRect>& bounds) {
    if (!bounds) return Array();
    return Array({Number(bounds->top), Number(bounds->left),
                  Number(bounds->bottom), Number(bounds->right)});
  };
  return Object({
      {"track_id", Number(layer.track_id)},
      {"layer", Number(layer.layer)},
      {"alternate_group", Number(layer.alternate_group)},
      {"track_sha256", String(Digest(layer.track_atom))},
      {"track_duration_ns", Number(selected.duration_ns.value_or(0))},
      {"track_size_fixed16", Array({Number(selected.clips.track_size[0]),
                                    Number(selected.clips.track_size[1])})},
      {"movie_clip", clip(selected.clips.movie)},
      {"movie_region_sha256",
       String(selected.clips.movie_region
                  ? Digest(selected.clips.movie_region->Bytes())
                  : "")},
      {"track_matrix", matrix(selected.matrices.track)},
      {"movie_matrix", matrix(selected.matrices.movie)},
  });
}
}  // namespace q4t::server::video_detail
