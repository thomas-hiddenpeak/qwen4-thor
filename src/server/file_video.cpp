#include "q4t/server/file_video.h"

#include <algorithm>

#include "file_video_affine.h"
#include "file_video_geometry.h"
#include "file_video_layer_record.h"
#include "file_video_projective.h"
#include "file_video_protocol.h"
#include "object_protocol.h"
#include "q4t/server/input_contract.h"
#include "q4t/server/media_admission.h"
#include "q4t/vision/media_plan.h"

namespace q4t::server {
namespace {
using namespace object_detail;
using Clock = std::chrono::steady_clock;
#ifdef Q4T_GRAMMAR_PROCESS_TESTS
thread_local std::string test_entry;
#endif
ObjectReply Invalid() {
  return Error(400, "invalid_file_video", "Invalid video container or frames");
}
ObjectReply Capacity() {
  return Error(413, "file_video_capacity", "Video preparation budget exceeded");
}
ObjectReply Dependency() {
  return Error(503, "file_video_dependency_unavailable",
               "Video codec unavailable");
}
ObjectReply Failed(GrammarFailure failure) {
  if (failure == GrammarFailure::kCancelled)
    return Error(499, "request_cancelled", "Video preparation cancelled");
  if (failure == GrammarFailure::kTimeout)
    return Error(408, "request_deadline_exceeded",
                 "Video preparation timed out");
  if (failure == GrammarFailure::kResource) return Capacity();
  if (failure == GrammarFailure::kUnavailable)
    return Error(503, "file_document_worker_capacity",
                 "Video codec slot occupied");
  return Error(503, "file_video_worker_failed", "Video codec worker failed");
}
bool Sha(std::string_view value) {
  return value.size() == 64 &&
         std::all_of(value.begin(), value.end(), [](char c) {
           return (c >= 'a' && c <= 'f') || (c >= '0' && c <= '9');
         });
}
bool Matrices(const Json& display, video_detail::DisplayMatrices* output,
              bool require_affine = true) {
  for (const auto& [name, matrix] :
       {std::pair{"movie_matrix", &output->movie},
        std::pair{"track_matrix", &output->track}}) {
    const auto* raw = display.GetArray(name);
    if (!raw || raw->array.size() != matrix->size()) return false;
    for (size_t i = 0; i < matrix->size(); ++i) {
      const auto& value = raw->array[i];
      if (!IsInputInteger(value) || value.number < INT32_MIN ||
          value.number > INT32_MAX)
        return false;
      (*matrix)[i] = int32_t(value.number);
    }
    if (require_affine && !video_detail::IsAffine(*matrix)) return false;
  }
  return true;
}
bool RectClips(const Json& display, bool fixed,
               video_detail::DisplayClips* clips) {
  for (const auto& [name, output] : {std::pair{"movie_clip", &clips->movie},
                                     std::pair{"track_clip", &clips->track}}) {
    const auto* raw = display.GetArray(name);
    if (!raw) return false;
    if (raw->array.empty()) continue;
    if (raw->array.size() != 4) return false;
    std::array<int16_t, 4> coords;
    for (size_t i = 0; i < coords.size(); ++i) {
      const auto& value = raw->array[i];
      if (!IsInputInteger(value) || value.number < INT16_MIN ||
          value.number > INT16_MAX)
        return false;
      coords[i] = static_cast<int16_t>(value.number);
    }
    if (coords[0] > coords[2] || coords[1] > coords[3]) return false;
    *output = {coords[0], coords[1], coords[2], coords[3]};
  }
  const auto* size =
      display.GetArray(fixed ? "track_size_fixed16" : "track_size");
  if (!size || size->array.size() != 2) return false;
  for (size_t i = 0; i < 2; ++i) {
    const auto& extent = size->array[i];
    if (!IsInputInteger(extent) || extent.number < 1 ||
        extent.number > (fixed ? UINT32_MAX : UINT16_MAX))
      return false;
    clips->track_size[i] = uint32_t(extent.number) << (fixed ? 0 : 16);
  }
  return fixed || clips->Any();
}
bool Integer(const Json* value, int64_t minimum, int64_t maximum) {
  return value && IsInputInteger(*value) && value->number >= minimum &&
         value->number <= maximum;
}
ObjectReply ValidateMovieLayers(const FileVideoLimits& limits,
                                const FileVideoPixels& pixels) {
  using namespace video_detail;
  const auto* geometry = pixels.conversion.Find("display_geometry");
  const auto* layers = geometry ? geometry->GetArray("layers") : nullptr;
  if (!layers || layers->array.empty() || geometry->object.size() != 4 ||
      geometry->GetString("policy") !=
          "quicktime-active-layers-copy-bilinear16")
    return Invalid();
  std::set<uint32_t> ids;
  std::set<uint16_t> groups;
  int64_t previous_layer = INT16_MAX;
  uint64_t decoded_frames = 0, decoded_pixels = 0, max_duration = 0;
  uint64_t canvases = pixels.frames.size();
  if (pixels.frames.front().timestamp_ns != 0) return Invalid();
  for (size_t i = 0; i < pixels.frames.size(); ++i) {
    const auto& frame = pixels.frames[i];
    const bool last = i + 1 == pixels.frames.size();
    const auto next =
        last ? pixels.duration_ns : pixels.frames[i + 1].timestamp_ns;
    if (!frame.duration_ns || next <= frame.timestamp_ns ||
        frame.duration_ns > next - frame.timestamp_ns ||
        ((last ||
          pixels.frames[i + 1].source_index == frame.source_index + 1) &&
         frame.duration_ns != next - frame.timestamp_ns))
      return Invalid();
  }
  MovieCanvas canvas;
  for (const auto& row : layers->array) {
    const auto* source = row.Find("source");
    if (!source || row.object.size() != 8 || source->object.size() != 10 ||
        !Integer(source->Find("track_id"), 1, UINT32_MAX) ||
        !Integer(source->Find("layer"), INT16_MIN, previous_layer) ||
        !Integer(source->Find("alternate_group"), 0, UINT16_MAX) ||
        !Sha(source->GetString("track_sha256")) ||
        !Integer(source->Find("track_duration_ns"), 0, pixels.duration_ns) ||
        !Integer(row.Find("duration_ns"), 1, pixels.duration_ns) ||
        !Integer(row.Find("scanned_frames"), 1, limits.max_scanned_frames))
      return Invalid();
    previous_layer = source->GetInt("layer");
    const auto group = source->GetInt("alternate_group");
    if (!ids.insert(source->GetInt("track_id")).second ||
        (group && !groups.insert(group).second))
      return Invalid();
    const auto duration = uint64_t(row.GetInt("duration_ns"));
    if (const auto declared = source->GetInt("track_duration_ns");
        declared && uint64_t(declared) != duration)
      return Invalid();
    max_duration = std::max(max_duration, duration);
    DisplayMatrices matrices;
    if (!Matrices(*source, &matrices, false)) return Invalid();
    const auto* clip = source->GetArray("movie_clip");
    if (!clip || (!clip->array.empty() && clip->array.size() != 4))
      return Invalid();
    if (!clip->array.empty()) {
      for (const auto& point : clip->array)
        if (!Integer(&point, INT16_MIN, INT16_MAX)) return Invalid();
      if (clip->array[0].number > clip->array[2].number ||
          clip->array[1].number > clip->array[3].number)
        return Invalid();
    }
    const auto* region = source->Find("movie_region_sha256");
    if (!region || !region->IsString() ||
        (!region->str.empty() && (clip->array.empty() || !Sha(region->str))))
      return Invalid();
    const auto* size = source->GetArray("track_size_fixed16");
    if (!size || size->array.size() != 2) return Invalid();
    TrackSize extent{};
    for (size_t i = 0; i < 2; ++i) {
      if (!Integer(&size->array[i], 0, UINT32_MAX)) return Invalid();
      extent[i] = uint32_t(size->array[i].number);
    }
    if ((extent[0] == 0) != (extent[1] == 0)) return Invalid();
    for (const auto* key : {"coded_width", "coded_height", "pixel_aspect_num",
                            "pixel_aspect_den"})
      if (!Integer(row.Find(key), 1, INT32_MAX)) return Invalid();
    DisplayGeometry decoded;
    auto status = SquarePixelGeometry(
        row.GetInt("coded_width"), row.GetInt("coded_height"),
        row.GetInt("pixel_aspect_num"), row.GetInt("pixel_aspect_den"), limits,
        &decoded);
    if (status == Failure::kNone && extent[0])
      status = UseTrackGrid(extent, limits, &decoded);
    ProjectiveGeometry projected;
    if (status == Failure::kNone)
      status = MakeProjectiveGeometry(matrices, decoded.square_width,
                                      decoded.square_height, limits, &projected,
                                      extent);
    if (status == Failure::kNone) status = canvas.Include(projected, limits);
    if (status != Failure::kNone)
      return status == Failure::kCapacity ? Capacity() : Invalid();
    const uint64_t count = 2 * uint64_t(row.GetInt("scanned_frames"));
    const auto per =
        std::max(uint64_t(decoded.coded_width) * decoded.coded_height,
                 uint64_t(decoded.square_width) * decoded.square_height);
    if (count > limits.max_scanned_frames - decoded_frames ||
        per > (limits.max_decoded_pixels - decoded_pixels) / count)
      return Capacity();
    decoded_frames += count;
    decoded_pixels += per * count;
    const auto* samples = row.GetArray("samples");
    if (!samples || samples->array.size() != pixels.frames.size())
      return Invalid();
    int64_t last = -1, start = 0, end = 0;
    for (size_t i = 0; i < samples->array.size(); ++i) {
      const auto& sample = samples->array[i];
      const auto time = pixels.frames[i].timestamp_ns;
      if (sample.IsNull()) {
        if (time < uint64_t(end)) return Invalid();
        continue;
      }
      if (!sample.IsArray() || sample.array.size() != 3 ||
          !Integer(&sample.array[0], 0, int64_t(count / 2) - 1) ||
          !Integer(&sample.array[1], 0, duration) ||
          !Integer(&sample.array[2], 1, duration))
        return Invalid();
      const int64_t index = sample.array[0].number,
                    begin = sample.array[1].number,
                    until = sample.array[2].number;
      if (index < last || time < uint64_t(begin) || time >= uint64_t(until) ||
          pixels.frames[i].duration_ns > uint64_t(until) - time ||
          (index == last && (begin != start || until != end)) ||
          (index > last && begin < end))
        return Invalid();
      last = index;
      start = begin;
      end = until;
      ++canvases;
    }
  }
  const auto area = uint64_t(canvas.width) * canvas.height;
  if (area > (limits.max_decoded_pixels - decoded_pixels) / canvases)
    return Capacity();
  decoded_pixels += area * canvases;
  if (canvas.width != pixels.width || canvas.height != pixels.height ||
      max_duration != pixels.duration_ns ||
      !Integer(geometry->Find("decoded_pixels"), decoded_pixels,
               decoded_pixels) ||
      !Integer(geometry->Find("decoded_frames"), decoded_frames,
               decoded_frames))
    return Invalid();
  return {};
}

uint64_t Big(std::string_view bytes) {
  uint64_t value = 0;
  for (unsigned char byte : bytes) value = (value << 8) | byte;
  return value;
}
// Only container lengths are inspected here. Codecs still own demuxing and
// payload validation; this prevents a known incomplete header becoming a
// stalled downstream sink with no video pad.
bool Element(std::string_view bytes, size_t* offset, uint64_t* id,
             uint64_t* length, bool* unknown) {
  const auto vint = [&](bool identifier, uint64_t* value,
                        size_t* width) -> bool {
    if (*offset >= bytes.size()) return false;
    const auto first = static_cast<unsigned char>(bytes[*offset]);
    unsigned char mask = 0x80;
    *width = 1;
    while (mask && !(first & mask)) {
      mask >>= 1;
      ++*width;
    }
    if (!mask || *width > (identifier ? 4 : 8) ||
        *width > bytes.size() - *offset)
      return false;
    *value = Big(bytes.substr(*offset, *width));
    if (!identifier) *value &= (uint64_t{1} << (7 * *width)) - 1;
    *offset += *width;
    return true;
  };
  size_t width;
  if (!vint(true, id, &width) || !vint(false, length, &width)) return false;
  *unknown = *length == (uint64_t{1} << (7 * width)) - 1;
  return true;
}
bool ContainerLengths(std::string_view bytes) {
  size_t offset = 0, elements = 0;
  if (bytes.size() >= 12 && bytes.substr(4, 4) == "ftyp") {
    bool movie = false, media = false;
    while (offset < bytes.size()) {
      if (++elements > 65536 || bytes.size() - offset < 8) return false;
      auto size = Big(bytes.substr(offset, 4));
      const auto type = bytes.substr(offset + 4, 4);
      size_t header = 8;
      if (size == 1) {
        if (bytes.size() - offset < 16) return false;
        size = Big(bytes.substr(offset + 8, 8));
        header = 16;
      } else if (!size) {
        size = bytes.size() - offset;
      }
      if (size < header || size > bytes.size() - offset ||
          (!offset && size < header + 8))
        return false;
      movie |= type == "moov";
      media |= type == "mdat";
      offset += size;
    }
    return movie && media;
  }
  uint64_t id = 0, length = 0;
  bool unknown = false;
  if (!Element(bytes, &offset, &id, &length, &unknown) || id != 0x1a45dfa3 ||
      unknown || length > bytes.size() - offset)
    return false;
  offset += length;
  while (offset < bytes.size()) {
    if (++elements > 65536 ||
        !Element(bytes, &offset, &id, &length, &unknown) ||
        (!unknown && length > bytes.size() - offset))
      return false;
    if (id == 0x18538067) {
      const size_t end = unknown ? bytes.size() : offset + length;
      while (offset < end) {
        if (++elements > 65536 ||
            !Element(bytes.substr(0, end), &offset, &id, &length, &unknown) ||
            (!unknown && length > end - offset))
          return false;
        // Streaming Clusters can have an unknown size. The codec must parse
        // their children and owns the remaining bounded deadline.
        if (unknown) return id == 0x1f43b675;
        offset += length;
      }
      return offset == bytes.size();
    }
    if (unknown || (id != 0xec && id != 0xbf)) return false;
    offset += length;
  }
  return false;
}
}  // namespace
FileVideoWorkBudget FileVideoPixels::Work() const {
  if (conversion.GetString("format") == "q4t.cpu_video_png.v9") {
    const auto* display = conversion.Find("display_geometry");
    if (!display) return {UINT64_MAX, UINT64_MAX};
    return {uint64_t(display->GetInt("decoded_frames", -1)),
            uint64_t(display->GetInt("decoded_pixels", -1))};
  }
  return {scanned_frames, scanned_frames * width * height};
}
bool IsVideoContainer(std::string_view bytes) {
  if (bytes.size() >= 12 && bytes.substr(4, 4) == "ftyp") {
    const auto brand = bytes.substr(8, 4);
    for (auto image :
         {"avif", "avis", "heic", "heix", "hevc", "hevx", "mif1", "msf1"})
      if (brand == image) return false;
    return true;
  }
  return bytes.size() >= 4 && bytes.substr(0, 4) == "\x1a\x45\xdf\xa3";
}
bool ValidFileVideoLimits(const FileVideoLimits& l) {
  return l.max_source_bytes && l.max_source_bytes <= kFileInputBytes &&
         l.max_edge && l.max_edge <= 8192 && l.max_frame_pixels &&
         l.max_frame_pixels <= (32ULL << 20) && l.max_decoded_pixels &&
         l.max_decoded_pixels <= (4ULL << 30) && l.max_output_bytes >= 1024 &&
         l.max_output_bytes <= (128ULL << 20) && l.max_duration_ns &&
         l.max_duration_ns <= 600000000000ULL && l.max_scanned_frames >= 2 &&
         l.max_scanned_frames <= 18000 && l.sample_frames >= 2 &&
         l.sample_frames <= 16 && l.max_thread_creations <= 64 &&
         l.timeout_ms >= 1 && l.timeout_ms <= 30000;
}
ObjectReply ValidateVideoPixels(std::string_view source,
                                const FileVideoLimits& limits,
                                const FileVideoPixels& pixels) {
  if (!IsVideoContainer(source) ||
      pixels.conversion.GetInt("source_bytes", -1) !=
          static_cast<int64_t>(source.size()))
    return Invalid();
  const auto format = pixels.conversion.GetString("format");
  if (format == "q4t.cpu_video_png.v1" || format == "q4t.cpu_video_png.v2" ||
      format == "q4t.cpu_video_png.v3") {
    try {
      const auto movie = video_detail::MovieDisplay().ReadPresentation(source);
      if (movie && movie->enabled.size() > 1) return Invalid();
    } catch (...) {
      return Invalid();
    }
  }
  if (format == "q4t.cpu_video_png.v9") {
    try {
      video_detail::MovieDisplay reader;
      const auto presentation = reader.ReadPresentation(source);
      const auto* display = pixels.conversion.Find("display_geometry");
      const auto* recorded = display ? display->GetArray("layers") : nullptr;
      if (!presentation || presentation->enabled.size() < 2 || !recorded)
        return Invalid();
      const auto actual = reader.PlaybackLayers(*presentation);
      if (recorded->array.size() != actual.size()) return Invalid();
      for (size_t i = 0; i < actual.size(); ++i) {
        const auto* binding = recorded->array[i].Find("source");
        if (!binding || reader.GraphicsMode(actual[i]) != 0) return Invalid();
        const auto expected = video_detail::LayerSource(
            actual[i], reader.DescribeLayer(*presentation, actual[i]));
        // Each source field is a scalar or an ordered array.
        if (binding->object.size() != expected.object.size()) return Invalid();
        for (const auto& [key, value] : expected.object) {
          const auto* found = binding->Find(key);
          if (!found || OutputJson(*found) != OutputJson(value))
            return Invalid();
        }
      }
    } catch (...) {
      return Invalid();
    }
  }
  const bool grid = format == "q4t.cpu_video_png.v8";
  const bool complex = format == "q4t.cpu_video_png.v7";
  const bool clipped = grid || complex || format == "q4t.cpu_video_png.v6";
  if (clipped || format == "q4t.cpu_video_png.v4" ||
      format == "q4t.cpu_video_png.v5") {
    const auto* display = pixels.conversion.Find("display_geometry");
    video_detail::DisplayMatrices recorded;
    if (!display ||
        !Matrices(*display, &recorded, format == "q4t.cpu_video_png.v4"))
      return Invalid();
    try {
      const auto actual =
          video_detail::MovieDisplay().SelectTrack(source, clipped);
      if (!actual || actual->matrices != recorded) return Invalid();
      video_detail::DisplayClips clips;
      if (clipped) {
        const auto& original = actual->clips;
        if (!RectClips(*display, grid, &clips) ||
            clips.movie != original.movie || clips.track != original.track ||
            clips.track_size != original.track_size ||
            (!grid && complex != original.Complex()))
          return Invalid();
        if (complex || grid) {
          for (const auto& [key, region] :
               {std::pair{"movie_region_sha256", &original.movie_region},
                std::pair{"track_region_sha256", &original.track_region}}) {
            if (display->GetString(key) !=
                (*region ? Digest((*region)->Bytes()) : ""))
              return Invalid();
          }
        }
      }
    } catch (...) {
      return Invalid();
    }
  }
  return ValidateVideoSnapshot(Digest(source), limits, pixels);
}
ObjectReply ValidateVideoSnapshot(std::string_view source_sha256,
                                  const FileVideoLimits& limits,
                                  const FileVideoPixels& pixels) {
  if (!ValidFileVideoLimits(limits))
    return Error(500, "file_video_budget_invalid", "Invalid video budget");
  if (!Sha(source_sha256) || !pixels.width || !pixels.height ||
      !pixels.duration_ns || pixels.frames.size() < 2 ||
      pixels.scanned_frames < pixels.frames.size())
    return Invalid();
  const auto* source_bytes = pixels.conversion.Find("source_bytes");
  if (!source_bytes || !IsInputInteger(*source_bytes) ||
      source_bytes->number <= 0)
    return Invalid();
  const bool layers =
      pixels.conversion.GetString("format") == "q4t.cpu_video_png.v9";
  if (source_bytes->number > limits.max_source_bytes ||
      pixels.width > limits.max_edge || pixels.height > limits.max_edge ||
      uint64_t{pixels.width} * pixels.height > limits.max_frame_pixels ||
      pixels.duration_ns > limits.max_duration_ns ||
      pixels.scanned_frames > limits.max_scanned_frames ||
      (!layers && uint64_t{pixels.width} * pixels.height >
                      limits.max_decoded_pixels / pixels.scanned_frames) ||
      pixels.frames.size() > limits.sample_frames)
    return Capacity();
  const auto& conversion = pixels.conversion;
  if (OutputJson(conversion).size() > video_detail::kMaxMetadataBytes)
    return Invalid();
  const auto* outputs = conversion.GetArray("frames");
  const auto* modules = conversion.GetArray("modules");
  const bool affine = conversion.GetString("format") == "q4t.cpu_video_png.v4";
  const bool projective =
      conversion.GetString("format") == "q4t.cpu_video_png.v5";
  const bool grid = conversion.GetString("format") == "q4t.cpu_video_png.v8";
  const bool complex = conversion.GetString("format") == "q4t.cpu_video_png.v7";
  const bool clipped = grid || complex ||
                       conversion.GetString("format") == "q4t.cpu_video_png.v6";
  const bool transformed = affine || projective || clipped;
  const bool geometry =
      transformed || conversion.GetString("format") == "q4t.cpu_video_png.v3";
  const bool intervals =
      layers || geometry ||
      conversion.GetString("format") == "q4t.cpu_video_png.v2";
  if ((!intervals &&
       conversion.GetString("format") != "q4t.cpu_video_png.v1") ||
      (!geometry && !layers && conversion.Has("display_geometry")) ||
      conversion.GetString("source_sha256") != source_sha256 ||
      conversion.GetString("sampling") !=
          "uniform-presentation-time-with-endpoints" ||
      !outputs || outputs->array.size() != pixels.frames.size() || !modules ||
      modules->array.empty() || modules->array.size() > 16)
    return Invalid();
  if (layers) {
    const auto checked = ValidateMovieLayers(limits, pixels);
    if (!checked.ok()) return checked;
  }
  if (geometry) {
    const auto* display = conversion.Find("display_geometry");
    if (!display || !display->IsObject() ||
        display->object.size() != ((grid || complex) ? 12U
                                   : clipped         ? 10U
                                   : transformed     ? 7U
                                                     : 5U) ||
        display->GetString("policy") !=
            (grid      ? "quicktime-fixed-track-grid-bilinear16"
             : complex ? "quicktime-region-clips-black-bilinear16"
             : clipped ? "quicktime-rect-clips-black-bilinear16"
             : projective
                 ? "projective-square-pixels-tight-integer512-bilinear16"
             : affine ? "affine-square-pixels-tight-bilinear16"
                      : "source-orientation-square-pixels"))
      return Invalid();
    for (const char* name : {"coded_width", "coded_height", "pixel_aspect_num",
                             "pixel_aspect_den"}) {
      const auto* value = display->Find(name);
      if (!value || !IsInputInteger(*value)) return Invalid();
    }
    video_detail::DisplayGeometry shape;
    const auto failure = video_detail::SquarePixelGeometry(
        display->GetInt("coded_width"), display->GetInt("coded_height"),
        display->GetInt("pixel_aspect_num"),
        display->GetInt("pixel_aspect_den"), limits, &shape);
    if (failure == video_detail::Failure::kCapacity) return Capacity();
    if (failure != video_detail::Failure::kNone) return Invalid();
    video_detail::DisplayClips clips;
    if (clipped) {
      if (!RectClips(*display, grid, &clips)) return Invalid();
      if (complex || grid) {
        bool has_region = false;
        for (const auto& [key, box] :
             {std::pair{"movie_region_sha256", &clips.movie},
              std::pair{"track_region_sha256", &clips.track}}) {
          const auto* digest = display->Find(key);
          if (!digest || !digest->IsString()) return Invalid();
          if (digest->str.empty()) continue;
          if (!Sha(digest->str) || !*box || (*box)->left == (*box)->right ||
              (*box)->top == (*box)->bottom)
            return Invalid();
          has_region = true;
        }
        if (complex && !has_region) return Invalid();
      }
      const auto status =
          video_detail::UseTrackGrid(clips.track_size, limits, &shape);
      if (status == video_detail::Failure::kCapacity) return Capacity();
      if (status != video_detail::Failure::kNone) return Invalid();
      if (uint64_t(shape.coded_width) * shape.coded_height >
          limits.max_decoded_pixels / pixels.scanned_frames)
        return Capacity();
    }
    if (uint64_t(shape.square_width) * shape.square_height >
        limits.max_decoded_pixels / pixels.scanned_frames)
      return Capacity();
    if (transformed) {
      video_detail::DisplayMatrices matrices;
      if (!Matrices(*display, &matrices, affine)) return Invalid();
      video_detail::Failure status;
      uint32_t width = 0, height = 0;
      if (video_detail::FractionalTrack(clips.track_size) || projective ||
          !video_detail::IsAffine(matrices.movie) ||
          !video_detail::IsAffine(matrices.track)) {
        video_detail::ProjectiveGeometry output;
        status = video_detail::MakeProjectiveGeometry(
            matrices, shape.square_width, shape.square_height, limits, &output,
            video_detail::FractionalTrack(clips.track_size)
                ? clips.track_size
                : video_detail::TrackSize{});
        width = output.width;
        height = output.height;
      } else {
        video_detail::AffineGeometry output;
        status = video_detail::MakeAffineGeometry(
            matrices, shape.square_width, shape.square_height, limits, &output);
        width = output.width;
        height = output.height;
      }
      if (status == video_detail::Failure::kCapacity) return Capacity();
      if (status != video_detail::Failure::kNone || width != pixels.width ||
          height != pixels.height)
        return Invalid();
    } else if (!((pixels.width == shape.square_width &&
                  pixels.height == shape.square_height) ||
                 (pixels.width == shape.square_height &&
                  pixels.height == shape.square_width)))
      return Invalid();
  }
  for (const auto& [name, expected] :
       std::vector<std::pair<const char*, uint64_t>>{
           {"width", pixels.width},
           {"height", pixels.height},
           {"duration_ns", pixels.duration_ns},
           {"scanned_frames", pixels.scanned_frames}}) {
    const auto* value = conversion.Find(name);
    if (!value || !IsInputInteger(*value) || value->number != expected)
      return Invalid();
  }
  for (const auto& module : modules->array) {
    const auto name = module.GetString("name"),
               version = module.GetString("version");
    if (name.empty() || name.size() > 64 || version.empty() ||
        version.size() > 64 || !Sha(module.GetString("sha256")))
      return Invalid();
  }
  uint64_t bytes = 0, previous_time = 0, previous_index = 0;
  for (size_t i = 0; i < pixels.frames.size(); ++i) {
    const auto& frame = pixels.frames[i];
    vision::MediaHeader header;
    if (frame.png.size() > limits.max_output_bytes - bytes) return Capacity();
    bytes += frame.png.size();
    if (!vision::ReadMediaHeader(
            {reinterpret_cast<const uint8_t*>(frame.png.data()),
             frame.png.size()},
            &header) ||
        header.format != vision::MediaFormat::kPng ||
        header.width != pixels.width || header.height != pixels.height ||
        frame.timestamp_ns >= pixels.duration_ns ||
        frame.duration_ns > pixels.duration_ns - frame.timestamp_ns ||
        frame.source_index >= pixels.scanned_frames ||
        (i && (frame.timestamp_ns <= previous_time ||
               frame.source_index <= previous_index)) ||
        outputs->array[i].GetString("sha256") != Digest(frame.png) ||
        outputs->array[i].GetInt("timestamp_ns", -1) !=
            static_cast<int64_t>(frame.timestamp_ns) ||
        outputs->array[i].GetInt("source_index", -1) !=
            static_cast<int64_t>(frame.source_index))
      return Invalid();
    for (const auto* name : {"timestamp_ns", "source_index"}) {
      const auto* value = outputs->array[i].Find(name);
      if (!value || !IsInputInteger(*value)) return Invalid();
    }
    const auto* duration = outputs->array[i].Find("duration_ns");
    if (intervals) {
      if (!duration || !IsInputInteger(*duration) || duration->number < 0 ||
          duration->number != frame.duration_ns)
        return Invalid();
    } else if (duration || frame.duration_ns) {
      return Invalid();
    }
    previous_time = frame.timestamp_ns;
    previous_index = frame.source_index;
  }
  if (pixels.frames.front().source_index != 0 ||
      pixels.frames.back().source_index + 1 != pixels.scanned_frames)
    return Invalid();
  return {};
}
Status MapPreparedVideoContent(const io::Json& part, io::Json* mapped,
                               std::string* error_param) {
  if (!mapped) return Status::Fail("Missing prepared video owner");
  *mapped = {};
  auto status = InputFields(part,
                            {"type", "video_frames", "video_timestamps",
                             "detail", "prompt_cache_breakpoint"},
                            error_param);
  const auto* frames = part.GetArray("video_frames");
  if (!status) return status;
  status = VideoInputDetail(part);
  if (!status) {
    if (error_param) *error_param = "detail";
    return status;
  }
  if (part.GetString("type") != "input_video_frames" || !frames ||
      frames->array.size() < 2 || frames->array.size() > 16 ||
      !part.Has("video_timestamps"))
    return Status::Fail("Invalid prepared video frames");
  for (const auto& frame : frames->array)
    if (!frame.IsString() || !frame.str.starts_with("data:image/png;base64,") ||
        frame.str.size() <= 22)
      return Status::Fail("Prepared video requires immutable PNG data");
  std::string field;
  status = VideoFrameTimes(part, frames->array.size(), nullptr, &field);
  if (!status) {
    if (error_param) *error_param = std::move(field);
    return status;
  }
  *mapped = part;
  Set(mapped, "type", String("video_frames"));
  return {};
}
#ifdef Q4T_GRAMMAR_PROCESS_TESTS
void detail::SetVideoWorkerEntryForTesting(std::string entry) {
  test_entry = std::move(entry);
}
#endif
ObjectReply DecodeVideoContainer(std::string_view source,
                                 const FileVideoLimits& limits,
                                 const BinaryAccess& access,
                                 FileVideoPixels* pixels,
                                 GrammarControl control) {
  if (!pixels) return Invalid();
  *pixels = {};
  if (!ValidFileVideoLimits(limits))
    return Error(500, "file_video_budget_invalid", "Invalid video budget");
  if (source.size() > limits.max_source_bytes) return Capacity();
  if (!IsVideoContainer(source) || !ContainerLengths(source)) return Invalid();
  if (access.scope.empty())
    return Error(503, "object_scope_unavailable",
                 "Authenticated video scope required");
  const auto cancelled = [&] {
    return (access.cancelled && access.cancelled()) ||
           (control.cancelled && control.cancelled());
  };
  if (cancelled()) return Failed(GrammarFailure::kCancelled);
  const auto deadline =
      std::min(access.deadline,
               Clock::now() + std::chrono::milliseconds(limits.timeout_ms));
  if (Clock::now() >= deadline) return Failed(GrammarFailure::kTimeout);
  control.domain = IsolatedWorkerDomain::kFileParse;
  control.compile_deadline = deadline;
  std::unique_ptr<detail::GrammarProcess> process;
  GrammarFailure failure;
  const char* entry = "--file-video-worker";
#ifdef Q4T_GRAMMAR_PROCESS_TESTS
  if (!test_entry.empty()) entry = test_entry.c_str();
#endif
  if (!detail::GrammarProcess::Start(entry, control, &process, &failure))
    return Failed(failure);
  video_detail::Request request{video_detail::kMagic, source.size(), limits};
  video_detail::Ready ready{};
  auto transfer = [&](void* data, size_t size, bool write) {
    return process->Transfer(data, size, write, deadline, cancelled);
  };
  if (!transfer(&request, sizeof(request), true) ||
      !transfer(const_cast<char*>(source.data()), source.size(), true) ||
      !transfer(&ready, sizeof(ready), false))
    return Failed(process->Failure());
  if (ready == video_detail::Ready::kDependency) return Dependency();
  if (ready == video_detail::Ready::kSandbox)
    return Error(503, "file_video_isolation_unavailable",
                 "Video isolation unavailable");
  if (ready == video_detail::Ready::kInvalid) return Invalid();
  if (ready != video_detail::Ready::kReady)
    return Failed(GrammarFailure::kWorker);
  if (!process->ReceiveIoBudget(
          {.write_bytes = 0,
           .file_creations = 0,
           .thread_creations = limits.max_thread_creations,
           .notifications = 128},
          deadline, cancelled))
    return Failed(process->Failure());
  if (control.worker_ready) control.worker_ready();
  video_detail::Result result;
  if (!transfer(&result, sizeof(result), false))
    return Failed(process->Failure());
  if (result.magic != video_detail::kMagic)
    return Failed(GrammarFailure::kWorker);
  if (result.failure == video_detail::Failure::kDependency) return Dependency();
  if (result.failure == video_detail::Failure::kCapacity) return Capacity();
  if (result.failure == video_detail::Failure::kInvalid) return Invalid();
  if (result.failure != video_detail::Failure::kNone || result.frames < 2 ||
      result.frames > limits.sample_frames || !result.metadata_bytes ||
      result.metadata_bytes > video_detail::kMaxMetadataBytes ||
      result.width > limits.max_edge || result.height > limits.max_edge)
    return Failed(GrammarFailure::kWorker);
  std::string metadata(result.metadata_bytes, '\0');
  if (!transfer(metadata.data(), metadata.size(), false))
    return Failed(process->Failure());
  FileVideoPixels next;
  if (!io::ParseJson(metadata, &next.conversion, true,
                     {.max_depth = 8,
                      .max_values = video_detail::kMaxMetadataBytes / 2 + 1,
                      .max_string_bytes = 16384,
                      .reject_duplicate_keys = true}))
    return Failed(GrammarFailure::kWorker);
  next.width = result.width;
  next.height = result.height;
  next.duration_ns = result.duration_ns;
  next.scanned_frames = result.scanned_frames;
  uint64_t bytes = 0;
  for (size_t i = 0; i < result.frames; ++i) {
    video_detail::Frame frame;
    if (!transfer(&frame, sizeof(frame), false))
      return Failed(process->Failure());
    if (!frame.bytes || frame.bytes > limits.max_output_bytes - bytes)
      return Failed(GrammarFailure::kWorker);
    bytes += frame.bytes;
    FileVideoFrame decoded{std::string(frame.bytes, '\0'), frame.timestamp_ns,
                           frame.source_index, frame.duration_ns};
    if (!transfer(decoded.png.data(), decoded.png.size(), false))
      return Failed(process->Failure());
    next.frames.push_back(std::move(decoded));
  }
  std::string trailing;
  if (!process->CollectOutput(&trailing, 0, deadline, cancelled))
    return Failed(process->Failure());
  const auto format = next.conversion.GetString("format");
  if (format != "q4t.cpu_video_png.v8" && format != "q4t.cpu_video_png.v9") {
    try {
      const auto selected = video_detail::MovieDisplay().SelectTrack(source);
      if (selected) {
        const auto& clips = selected->clips;
        if (video_detail::FractionalTrack(clips.track_size)) return Invalid();
        if (clips.Any()) {
          if (format != "q4t.cpu_video_png.v6" &&
              format != "q4t.cpu_video_png.v7")
            return Invalid();
        } else if (clips.track_size[0]) {
          const auto* d = next.conversion.Find("display_geometry");
          video_detail::DisplayGeometry natural;
          if (!d ||
              video_detail::SquarePixelGeometry(
                  d->GetInt("coded_width"), d->GetInt("coded_height"),
                  d->GetInt("pixel_aspect_num"), d->GetInt("pixel_aspect_den"),
                  limits, &natural) != video_detail::Failure::kNone ||
              clips.track_size !=
                  video_detail::TrackSize{natural.square_width << 16,
                                          natural.square_height << 16})
            return Invalid();
        }
      }
    } catch (...) {
      return Invalid();
    }
  }
  const auto valid = ValidateVideoPixels(source, limits, next);
  if (!valid.ok()) return Failed(GrammarFailure::kWorker);
  if (cancelled()) return Failed(GrammarFailure::kCancelled);
  if (Clock::now() >= deadline) return Failed(GrammarFailure::kTimeout);
  *pixels = std::move(next);
  return {};
}
}  // namespace q4t::server
