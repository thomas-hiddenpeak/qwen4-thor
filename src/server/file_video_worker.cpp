#include "q4t/server/file_video.h"

#include <dlfcn.h>
#include <fcntl.h>
#include <sys/resource.h>
#include <sys/stat.h>
#include <unistd.h>

#include <atomic>
#include <cstring>
#include <mutex>

#include "file_video_affine.h"
#include "file_video_geometry.h"
#include "file_video_orientation.h"
#include "file_video_projective.h"
#include "file_video_protocol.h"
#include "media_codec_runtime.h"
#include "object_protocol.h"
#include "q4t/server/lifetime.h"
#include "q4t/vision/media_plan.h"

#ifdef Q4T_HAVE_GSTREAMER_HEADERS
#include <gst/app/gstappsink.h>
#include <gst/app/gstappsrc.h>
#include <gst/gst.h>
#include <gst/video/video.h>
#endif

namespace q4t::server {
namespace {
using namespace object_detail;
using namespace video_detail;
#ifdef Q4T_HAVE_GSTREAMER_HEADERS
using media_detail::Runtime;
struct TransformedSize {
  uint32_t width = 0, height = 0;
};
Failure GetTransformedSize(const DisplayMatrices& matrices, uint32_t width,
                          uint32_t height, const FileVideoLimits& limits,
                          TransformedSize* result) {
  if (IsAffine(matrices.movie) && IsAffine(matrices.track)) {
    AffineGeometry geometry;
    const auto status =
        MakeAffineGeometry(matrices, width, height, limits, &geometry);
    *result = {geometry.width, geometry.height};
    return status;
  }
  ProjectiveGeometry geometry;
  const auto status =
      MakeProjectiveGeometry(matrices, width, height, limits, &geometry);
  *result = {geometry.width, geometry.height};
  return status;
}
// Reuse the already loaded PNG plugin. A single bounded pipeline serves all
// frames of this request; no per-frame process or thread-pool proliferation.
class PngEncoder {
 public:
  explicit PngEncoder(Runtime& api) : api_(api) {}
  ~PngEncoder() {
    if (pipeline_) {
      api_.gst_element_set_state_fn(pipeline_, GST_STATE_NULL);
      api_.gst_object_unref_fn(pipeline_);
    }
  }
  Failure Encode(uint32_t width, uint32_t height,
                 const std::vector<uint8_t>& rgb, uint64_t max_bytes,
                 std::string* png) {
    if (!pipeline_) {
      const auto status = Initialize(width, height);
      if (status != Failure::kNone) return status;
    }
    if (int(width) != info_.width || int(height) != info_.height ||
        rgb.size() != size_t(width) * height * 3)
      return Failure::kInvalid;
    auto* buffer =
        api_.gst_buffer_new_allocate_fn(nullptr, info_.size, nullptr);
    if (!buffer) return Failure::kCapacity;
    for (uint32_t row = 0; row < height; ++row) {
      if (api_.gst_buffer_fill_fn(
              buffer, info_.offset[0] + size_t(row) * info_.stride[0],
              rgb.data() + size_t(row) * width * 3, width * 3) != width * 3) {
        api_.Mini(buffer);
        return Failure::kInvalid;
      }
    }
    GST_BUFFER_PTS(buffer) = frames_++ * GST_SECOND;
    GST_BUFFER_DURATION(buffer) = GST_SECOND;
    if (api_.gst_app_src_push_buffer_fn(reinterpret_cast<GstAppSrc*>(input_),
                                        buffer) != GST_FLOW_OK)
      return Failure::kInvalid;
    auto* sample = api_.gst_app_sink_try_pull_sample_fn(
        reinterpret_cast<GstAppSink*>(output_), 5 * GST_SECOND);
    if (!sample) return Failure::kInvalid;
    ScopeExit release([&] { api_.Mini(sample); });
    auto* encoded = api_.gst_sample_get_buffer_fn(sample);
    if (!encoded) return Failure::kInvalid;
    const auto size = api_.gst_buffer_get_size_fn(encoded);
    if (!size || size > max_bytes) return Failure::kCapacity;
    png->resize(size);
    return api_.gst_buffer_extract_fn(encoded, 0, png->data(), size) == size
               ? Failure::kNone
               : Failure::kInvalid;
  }

 private:
  Failure Initialize(uint32_t width, uint32_t height) {
    if (!api_.gst_video_info_set_format_fn(&info_, GST_VIDEO_FORMAT_RGB, width,
                                           height))
      return Failure::kInvalid;
    pipeline_ = api_.gst_pipeline_new_fn(nullptr);
    if (!pipeline_) return Failure::kCapacity;
    std::vector<GstElement*> nodes;
    for (const char* name : {"appsrc", "pngenc", "appsink"}) {
      auto* node = api_.gst_element_factory_make_fn(name, nullptr);
      if (!node) return Failure::kDependency;
      if (!api_.gst_bin_add_fn(reinterpret_cast<GstBin*>(pipeline_), node)) {
        api_.gst_object_unref_fn(node);
        return Failure::kInvalid;
      }
      nodes.push_back(node);
    }
    input_ = nodes[0];
    output_ = nodes[2];
    const auto description =
        "video/x-raw,format=RGB,pixel-aspect-ratio=1/1,"
        "framerate=1/1,width=" +
        std::to_string(width) + ",height=" + std::to_string(height);
    auto* caps = api_.gst_caps_from_string_fn(description.c_str());
    if (!caps) return Failure::kCapacity;
    api_.g_object_set_fn(input_, "caps", caps, "format", GST_FORMAT_TIME,
                         "block", TRUE, "max-bytes", guint64(info_.size),
                         nullptr);
    api_.Mini(caps);
    api_.g_object_set_fn(output_, "sync", FALSE, "async", FALSE, "max-buffers",
                         guint{1}, "drop", FALSE, "wait-on-eos", FALSE,
                         nullptr);
    if (!api_.gst_element_link_fn(nodes[0], nodes[1]) ||
        !api_.gst_element_link_fn(nodes[1], nodes[2]) ||
        api_.gst_element_set_state_fn(pipeline_, GST_STATE_PLAYING) ==
            GST_STATE_CHANGE_FAILURE)
      return Failure::kInvalid;
    return Failure::kNone;
  }
  Runtime& api_;
  GstElement *pipeline_ = nullptr, *input_ = nullptr, *output_ = nullptr;
  GstVideoInfo info_{};
  uint64_t frames_ = 0;
};

Failure TransformSample(Runtime& api, GstSample* sample,
                        const DisplayMatrices& matrices,
                        const DisplayClips& clips, const DisplayGeometry& shape,
                        const FileVideoLimits& limits, PngEncoder* encoder,
                        std::string* png) {
  GstVideoInfo info{};
  auto* caps = api.gst_sample_get_caps_fn(sample);
  auto* buffer = api.gst_sample_get_buffer_fn(sample);
  if (!caps || !buffer || !api.gst_video_info_from_caps_fn(&info, caps) ||
      GST_VIDEO_INFO_FORMAT(&info) != GST_VIDEO_FORMAT_RGB ||
      info.width != int(shape.square_width) ||
      info.height != int(shape.square_height))
    return Failure::kInvalid;
  GstVideoFrame frame{};
  if (!api.gst_video_frame_map_fn(&frame, &info, buffer, GST_MAP_READ))
    return Failure::kInvalid;
  ScopeExit unmap([&] { api.gst_video_frame_unmap_fn(&frame); });
  const auto stride = GST_VIDEO_FRAME_PLANE_STRIDE(&frame, 0);
  const auto start = reinterpret_cast<uintptr_t>(frame.data[0]);
  const auto base = reinterpret_cast<uintptr_t>(frame.map[0].data);
  if (stride <= 0 || start < base || start - base > frame.map[0].size)
    return Failure::kInvalid;
  std::span<const uint8_t> view(static_cast<const uint8_t*>(frame.data[0]),
                                frame.map[0].size - (start - base));
  TransformedSize dimensions;
  std::vector<uint8_t> rgb;
  Failure status;
  if (IsAffine(matrices.movie) && IsAffine(matrices.track)) {
    AffineGeometry geometry;
    status = RenderAffineRgb(matrices, shape.square_width, shape.square_height,
                             view, stride, limits, &geometry, &rgb, clips);
    dimensions = {geometry.width, geometry.height};
  } else {
    ProjectiveGeometry geometry;
    status =
        RenderProjectiveRgb(matrices, shape.square_width, shape.square_height,
                            view, stride, limits, &geometry, &rgb, clips);
    dimensions = {geometry.width, geometry.height};
  }
  if (status != Failure::kNone) return status;
  return encoder->Encode(dimensions.width, dimensions.height, rgb,
                         limits.max_output_bytes, png);
}

struct Pipeline {
  Runtime& api;
  const Request& request;
  std::string_view source;
  Pipeline(Runtime& runtime, const Request& options, std::string_view bytes)
      : api(runtime), request(options), source(bytes) {}
  GstElement *pipeline = nullptr, *convert = nullptr, *square_caps = nullptr;
  std::mutex source_mutex;
  std::mutex geometry_mutex;
  DisplayGeometry geometry;
  const DisplayMatrices* transform = nullptr;
  const DisplayClips* clips = nullptr;
  size_t offset = 0;
  std::atomic<Failure> failure{Failure::kNone};
  bool video = false;
  bool has_alternates = false;
  DisplayGeometry Geometry() {
    std::lock_guard lock(geometry_mutex);
    return geometry;
  }
  static GstPadProbeReturn RawCaps(GstPad*, GstPadProbeInfo* info,
                                   gpointer opaque) noexcept {
    auto& self = *static_cast<Pipeline*>(opaque);
    auto* event = GST_PAD_PROBE_INFO_EVENT(info);
    if (!event || GST_EVENT_TYPE(event) != GST_EVENT_CAPS)
      return GST_PAD_PROBE_OK;
    try {
      GstCaps* caps = nullptr;
      self.api.gst_event_parse_caps_fn(event, &caps);
      return self.ConfigureGeometry(caps) ? GST_PAD_PROBE_OK
                                          : GST_PAD_PROBE_DROP;
    } catch (...) {
      self.failure = Failure::kCapacity;
      return GST_PAD_PROBE_DROP;
    }
  }
  bool ConfigureGeometry(GstCaps* caps) {
    if (!caps) {
      failure = Failure::kInvalid;
      return false;
    }
    const auto* shape = api.gst_caps_get_structure_fn(caps, 0);
    int width = 0, height = 0, numerator = 1, denominator = 1;
    if (!api.gst_structure_get_int_fn(shape, "width", &width) ||
        !api.gst_structure_get_int_fn(shape, "height", &height) ||
        (api.gst_structure_has_field_fn(shape, "pixel-aspect-ratio") &&
         !api.gst_structure_get_fraction_fn(shape, "pixel-aspect-ratio",
                                            &numerator, &denominator))) {
      failure = Failure::kInvalid;
      return false;
    }
    DisplayGeometry selected;
    const auto result = SquarePixelGeometry(
        width, height, numerator, denominator, request.limits, &selected);
    if (result != Failure::kNone) {
      failure = result;
      return false;
    }
    if (clips) {
      const auto status =
          UseTrackGrid(clips->track_size[0], clips->track_size[1],
                       request.limits, &selected);
      if (status != Failure::kNone) {
        failure = status;
        return false;
      }
    }
    if (transform) {
      TransformedSize output;
      const auto status = GetTransformedSize(*transform, selected.square_width,
                                             selected.square_height,
                                             request.limits, &output);
      if (status != Failure::kNone) {
        failure = status;
        return false;
      }
    }
    std::lock_guard lock(geometry_mutex);
    if (geometry.coded_width) {
      if (geometry != selected) failure = Failure::kInvalid;
      return geometry == selected;
    }
    const auto description =
        "video/x-raw,format=RGB,pixel-aspect-ratio=1/1,width=" +
        std::to_string(selected.square_width) +
        ",height=" + std::to_string(selected.square_height);
    auto* target = api.gst_caps_from_string_fn(description.c_str());
    if (!target) {
      failure = Failure::kCapacity;
      return false;
    }
    api.g_object_set_fn(square_caps, "caps", target, nullptr);
    api.Mini(target);
    geometry = selected;
    return true;
  }
  static void Need(GstAppSrc* src, guint requested, gpointer opaque) noexcept {
    auto& self = *static_cast<Pipeline*>(opaque);
    try {
      std::lock_guard lock(self.source_mutex);
      const auto size = std::min<size_t>({self.source.size() - self.offset,
                                          requested == static_cast<guint>(-1)
                                              ? size_t{65536}
                                              : size_t{requested},
                                          size_t{65536}});
      if (!size) {
        self.api.gst_app_src_end_of_stream_fn(src);
        return;
      }
      auto* buffer =
          self.api.gst_buffer_new_allocate_fn(nullptr, size, nullptr);
      if (!buffer) {
        self.failure = Failure::kCapacity;
        return;
      }
      if (self.api.gst_buffer_fill_fn(
              buffer, 0, self.source.data() + self.offset, size) != size) {
        self.api.Mini(buffer);
        self.failure = Failure::kInvalid;
        return;
      }
      GST_BUFFER_OFFSET(buffer) = self.offset;
      self.offset += size;
      GST_BUFFER_OFFSET_END(buffer) = self.offset;
      const auto pushed = self.api.gst_app_src_push_buffer_fn(src, buffer);
      if (pushed != GST_FLOW_OK && pushed != GST_FLOW_FLUSHING)
        self.failure = Failure::kInvalid;
    } catch (...) {
      self.failure = Failure::kCapacity;
    }
  }
  static gboolean Seek(GstAppSrc*, guint64 offset, gpointer opaque) noexcept {
    auto& self = *static_cast<Pipeline*>(opaque);
    std::lock_guard lock(self.source_mutex);
    if (offset > self.source.size()) return false;
    self.offset = offset;
    return true;
  }
  static void Pad(GstElement*, GstPad* pad, gpointer opaque) noexcept {
    auto& self = *static_cast<Pipeline*>(opaque);
    try {
      self.Link(pad);
    } catch (...) {
      self.failure = Failure::kCapacity;
    }
  }
  void Link(GstPad* pad) {
    auto* caps = api.gst_pad_get_current_caps_fn(pad);
    if (!caps) {
      failure = Failure::kInvalid;
      return;
    }
    ScopeExit release([&] { api.Mini(caps); });
    const auto* shape = api.gst_caps_get_structure_fn(caps, 0);
    const char* mime = api.gst_structure_get_name_fn(shape);
    if (!mime || !std::string_view(mime).starts_with("video/")) return;
    if (has_alternates) {
      // qtdemux marks disabled tracks in the stream-start sticky event
      // before exposing the pad. Pad order need not match the default track.
      auto* start =
          api.gst_pad_get_sticky_event_fn(pad, GST_EVENT_STREAM_START, 0);
      if (!start) {
        failure = Failure::kInvalid;
        return;
      }
      GstStreamFlags flags = GST_STREAM_FLAG_NONE;
      api.gst_event_parse_stream_flags_fn(start, &flags);
      api.Mini(start);
      if (flags & GST_STREAM_FLAG_UNSELECT) return;
    }
    if (video) {
      failure = Failure::kInvalid;
      return;
    }
    video = true;
    int w = 0, h = 0;
    if (!api.gst_structure_get_int_fn(shape, "width", &w) ||
        !api.gst_structure_get_int_fn(shape, "height", &h) || w <= 0 ||
        h <= 0) {
      failure = Failure::kInvalid;
      return;
    }
    if (static_cast<uint32_t>(w) > request.limits.max_edge ||
        static_cast<uint32_t>(h) > request.limits.max_edge ||
        uint64_t(w) * h > request.limits.max_frame_pixels) {
      failure = Failure::kCapacity;
      return;
    }
    const std::string_view type(mime);
    const char* decoder = type == "video/x-h264"  ? "openh264dec"
                          : type == "video/x-vp8" ? "vp8dec"
                          : type == "video/x-vp9" ? "vp9dec"
                                                  : nullptr;
    if (!decoder) {
      failure = Failure::kDependency;
      return;
    }
    auto* decode = api.gst_element_factory_make_fn(decoder, nullptr);
    auto* parser = type == "video/x-h264"
                       ? api.gst_element_factory_make_fn("h264parse", nullptr)
                       : nullptr;
    if (!decode || (type == "video/x-h264" && !parser)) {
      if (decode) api.gst_object_unref_fn(decode);
      if (parser) api.gst_object_unref_fn(parser);
      failure = Failure::kDependency;
      return;
    }
    if (type == "video/x-vp8" || type == "video/x-vp9")
      api.g_object_set_fn(decode, "threads", guint{1}, nullptr);
    auto* raw = api.gst_element_get_static_pad_fn(decode, "src");
    const auto probe =
        raw ? api.gst_pad_add_probe_fn(raw, GST_PAD_PROBE_TYPE_EVENT_DOWNSTREAM,
                                       RawCaps, this, nullptr)
            : 0;
    if (raw) api.gst_object_unref_fn(raw);
    if (!probe) {
      api.gst_object_unref_fn(decode);
      if (parser) api.gst_object_unref_fn(parser);
      failure = Failure::kInvalid;
      return;
    }
    if (!api.gst_bin_add_fn(reinterpret_cast<GstBin*>(pipeline), decode) ||
        (parser &&
         !api.gst_bin_add_fn(reinterpret_cast<GstBin*>(pipeline), parser)) ||
        !api.gst_element_link_fn(decode, convert) ||
        (parser && !api.gst_element_link_fn(parser, decode))) {
      failure = Failure::kInvalid;
      return;
    }
    auto* sink =
        api.gst_element_get_static_pad_fn(parser ? parser : decode, "sink");
    if (!sink) {
      failure = Failure::kInvalid;
      return;
    }
    const auto linked = api.gst_pad_link_fn(pad, sink);
    api.gst_object_unref_fn(sink);
    if (linked != GST_PAD_LINK_OK ||
        !api.gst_element_sync_state_with_parent_fn(decode) ||
        (parser && !api.gst_element_sync_state_with_parent_fn(parser)))
      failure = Failure::kInvalid;
  }
};
Failure Decode(Runtime& api, const Request& request, const std::string& source,
               FileVideoPixels* output) {
  const auto track = MovieDisplay().SelectTrack(source);
  const auto* matrices = track ? &track->matrices : nullptr;
  const auto direction = matrices ? matrices->Direction() : std::nullopt;
  const bool clipped = track && track->clips.Any();
  const bool transformed = matrices && (!direction || clipped);
  const bool projective = matrices &&
      (!IsAffine(matrices->movie) || !IsAffine(matrices->track));
  auto* pipeline = api.gst_pipeline_new_fn(nullptr);
  if (!pipeline) return Failure::kCapacity;
  ScopeExit destroy([&] {
    api.gst_element_set_state_fn(pipeline, GST_STATE_NULL);
    api.gst_object_unref_fn(pipeline);
  });
  std::vector<GstElement*> elements;
  for (const char* name :
       {"appsrc", source.substr(4, 4) == "ftyp" ? "qtdemux" : "matroskademux",
        "videoconvert", "videoscale", "capsfilter", "videoflip",
        transformed ? "identity" : "pngenc", "appsink"}) {
    auto* node = api.gst_element_factory_make_fn(name, nullptr);
    if (!node) return Failure::kDependency;
    if (!api.gst_bin_add_fn(reinterpret_cast<GstBin*>(pipeline), node)) {
      api.gst_object_unref_fn(node);
      return Failure::kInvalid;
    }
    elements.push_back(node);
  }
  Pipeline state{api, request, source};
  state.has_alternates = track && track->has_alternates;
  state.clips = clipped ? &track->clips : nullptr;
  if (clipped) api.g_object_set_fn(elements[3], "add-borders", FALSE, nullptr);
  state.pipeline = pipeline;
  state.convert = elements[2];
  state.square_caps = elements[4];
  state.transform = transformed ? &*matrices : nullptr;
  PngEncoder encoder(api);
  GstAppSrcCallbacks callbacks{};
  callbacks.need_data = Pipeline::Need;
  callbacks.seek_data = Pipeline::Seek;
  api.gst_app_src_set_callbacks_fn(reinterpret_cast<GstAppSrc*>(elements[0]),
                                   &callbacks, &state, nullptr);
  api.g_object_set_fn(
      elements[0], "stream-type", GST_APP_STREAM_TYPE_RANDOM_ACCESS, "format",
      GST_FORMAT_BYTES, "size", static_cast<gint64>(source.size()), "block",
      TRUE, "max-bytes", static_cast<guint64>(131072), nullptr);
  api.g_object_set_fn(elements[7], "sync", FALSE, "max-buffers", guint{1},
                      "drop", FALSE, "wait-on-eos", FALSE, nullptr);
  constexpr std::array<GstVideoOrientationMethod, 8> directions = {
      GST_VIDEO_ORIENTATION_IDENTITY, GST_VIDEO_ORIENTATION_90R,
      GST_VIDEO_ORIENTATION_180,      GST_VIDEO_ORIENTATION_90L,
      GST_VIDEO_ORIENTATION_HORIZ,    GST_VIDEO_ORIENTATION_VERT,
      GST_VIDEO_ORIENTATION_UL_LR,    GST_VIDEO_ORIENTATION_UR_LL};
  api.g_object_set_fn(elements[5], "video-direction",
                      transformed ? GST_VIDEO_ORIENTATION_IDENTITY
                      : direction ? directions[static_cast<size_t>(*direction)]
                                  : GST_VIDEO_ORIENTATION_AUTO,
                      nullptr);
  auto* caps = api.gst_caps_from_string_fn("video/x-raw,format=RGB");
  if (!caps) return Failure::kCapacity;
  api.g_object_set_fn(elements[4], "caps", caps, nullptr);
  api.Mini(caps);
  api.g_signal_connect_data_fn(elements[1], "pad-added",
                               reinterpret_cast<GCallback>(Pipeline::Pad),
                               &state, nullptr, GConnectFlags(0));
  if (!api.gst_element_link_fn(elements[0], elements[1]))
    return Failure::kInvalid;
  for (size_t i = 2; i + 1 < elements.size(); ++i)
    if (!api.gst_element_link_fn(elements[i], elements[i + 1]))
      return Failure::kInvalid;
  auto* bus = api.gst_element_get_bus_fn(pipeline);
  if (!bus) return Failure::kInvalid;
  ScopeExit release_bus([&] { api.gst_object_unref_fn(bus); });
  // Teardown must happen while callback state is alive.
  ScopeExit stop(
      [&] { api.gst_element_set_state_fn(pipeline, GST_STATE_NULL); });
  if (api.gst_element_set_state_fn(pipeline, GST_STATE_PLAYING) ==
      GST_STATE_CHANGE_FAILURE)
    return state.failure == Failure::kNone ? Failure::kInvalid
                                           : state.failure.load();
  uint64_t previous = 0, count = 0, output_bytes = 0;
  FileVideoFrame last;
  for (;;) {
    if (state.failure != Failure::kNone) return state.failure.load();
    auto* sample = api.gst_app_sink_try_pull_sample_fn(
        reinterpret_cast<GstAppSink*>(elements[7]), 100000000);
    if (!sample) {
      auto* message =
          api.gst_bus_timed_pop_filtered_fn(bus, 0, GST_MESSAGE_ERROR);
      if (message) {
        api.Mini(message);
        return state.failure == Failure::kNone ? Failure::kInvalid
                                               : state.failure.load();
      }
      if (api.gst_app_sink_is_eos_fn(
              reinterpret_cast<GstAppSink*>(elements[7])))
        break;
      continue;
    }
    ScopeExit release_sample([&] { api.Mini(sample); });
    auto* buffer = api.gst_sample_get_buffer_fn(sample);
    const auto geometry = state.Geometry();
    if (!buffer || !geometry.coded_width) return Failure::kInvalid;
    const auto* segment = api.gst_sample_get_segment_fn(sample);
    const auto timestamp =
        segment ? api.gst_segment_to_stream_time_fn(segment, GST_FORMAT_TIME,
                                                    GST_BUFFER_PTS(buffer))
                : GST_CLOCK_TIME_NONE;
    if (timestamp == GST_CLOCK_TIME_NONE || (count && timestamp <= previous))
      return Failure::kInvalid;
    previous = timestamp;
    if (!count) {
      if (track && track->duration_ns) {
        output->duration_ns = *track->duration_ns;
      } else {
        gint64 duration = 0;
        if (!api.gst_element_query_duration_fn(pipeline, GST_FORMAT_TIME,
                                               &duration) ||
            duration <= 0)
          return Failure::kInvalid;
        output->duration_ns = duration;
      }
      if (output->duration_ns > request.limits.max_duration_ns)
        return Failure::kCapacity;
    }
    if (timestamp >= output->duration_ns) return Failure::kInvalid;
    uint64_t frame_duration = 0;
    const auto reported_duration = GST_BUFFER_DURATION(buffer);
    if (reported_duration != GST_CLOCK_TIME_NONE && reported_duration) {
      const auto pts = GST_BUFFER_PTS(buffer);
      if (reported_duration >= GST_CLOCK_TIME_NONE - pts)
        return Failure::kInvalid;
      const auto end = api.gst_segment_to_stream_time_fn(
          segment, GST_FORMAT_TIME, pts + reported_duration);
      if (end != GST_CLOCK_TIME_NONE) {
        if (end <= timestamp || end > output->duration_ns)
          return Failure::kInvalid;
        frame_duration = end - timestamp;
      }
    }
    TransformedSize display_size;
    if (transformed) {
      const auto status = GetTransformedSize(*matrices, geometry.square_width,
                                             geometry.square_height,
                                             request.limits, &display_size);
      if (status != Failure::kNone) return status;
    }
    const uint64_t work_pixels =
        std::max({uint64_t(geometry.coded_width) * geometry.coded_height,
                  uint64_t(geometry.square_width) * geometry.square_height,
                  uint64_t(display_size.width) * display_size.height});
    if (++count > request.limits.max_scanned_frames ||
        work_pixels > request.limits.max_decoded_pixels / count)
      return Failure::kCapacity;
    last = {{}, timestamp, count - 1, frame_duration};
    if (transformed) {
      const auto status =
          TransformSample(api, sample, *matrices, track->clips, geometry,
                          request.limits, &encoder, &last.png);
      if (status != Failure::kNone) return status;
    } else {
      const auto size = api.gst_buffer_get_size_fn(buffer);
      if (!size || size > request.limits.max_output_bytes)
        return Failure::kCapacity;
      last.png.resize(size);
      if (api.gst_buffer_extract_fn(buffer, 0, last.png.data(), size) != size)
        return Failure::kInvalid;
    }
    const auto size = last.png.size();
    vision::MediaHeader header;
    if (!vision::ReadMediaHeader(
            {reinterpret_cast<const uint8_t*>(last.png.data()), size},
            &header) ||
        header.format != vision::MediaFormat::kPng ||
        (transformed ? (header.width != display_size.width ||
                        header.height != display_size.height)
                : !((header.width == geometry.square_width &&
                     header.height == geometry.square_height) ||
                    (header.width == geometry.square_height &&
                     header.height == geometry.square_width))))
      return Failure::kInvalid;
    if (count == 1) {
      output->width = header.width;
      output->height = header.height;
    } else if (output->width != header.width ||
               output->height != header.height) {
      return Failure::kInvalid;
    }
    const auto selected = output->frames.size();
    if (!selected || (selected + 1 < request.limits.sample_frames &&
                      timestamp >= output->duration_ns * selected /
                                       (request.limits.sample_frames - 1))) {
      if (size > request.limits.max_output_bytes - output_bytes)
        return Failure::kCapacity;
      output_bytes += size;
      output->frames.push_back(last);
    }
  }
  if (state.failure != Failure::kNone) return state.failure.load();
  if (count < 2 || last.png.empty()) return Failure::kInvalid;
  if (output->frames.back().source_index != last.source_index) {
    if (last.png.size() > request.limits.max_output_bytes - output_bytes)
      return Failure::kCapacity;
    output->frames.push_back(std::move(last));
  }
  output->scanned_frames = count;
  auto frames = Array();
  for (const auto& frame : output->frames)
    frames.array.push_back(
        Object({{"sha256", String(Digest(frame.png))},
                {"timestamp_ns", Number(frame.timestamp_ns)},
                {"duration_ns", Number(frame.duration_ns)},
                {"source_index", Number(frame.source_index)}}));
  const auto geometry = state.Geometry();
  output->conversion = Object(
      {{"format", String("q4t.cpu_video_png.v3")},
       {"source_sha256", String(Digest(source))},
       {"source_bytes", Number(source.size())},
       {"sampling", String("uniform-presentation-time-with-endpoints")},
       {"width", Number(output->width)},
       {"height", Number(output->height)},
       {"duration_ns", Number(output->duration_ns)},
       {"scanned_frames", Number(output->scanned_frames)},
       {"display_geometry",
        Object({{"policy", String("source-orientation-square-pixels")},
                {"coded_width", Number(geometry.coded_width)},
                {"coded_height", Number(geometry.coded_height)},
                {"pixel_aspect_num", Number(geometry.pixel_aspect_num)},
                {"pixel_aspect_den", Number(geometry.pixel_aspect_den)}})},
       {"modules", api.modules},
       {"frames", std::move(frames)}});
  if (transformed) {
    Set(&output->conversion, "format",
        String(clipped      ? "q4t.cpu_video_png.v6"
               : projective ? "q4t.cpu_video_png.v5"
                            : "q4t.cpu_video_png.v4"));
    auto& display = Field(output->conversion, "display_geometry");
    Set(&display, "policy",
        String(clipped ? "quicktime-rect-clips-black-bilinear16"
               : projective
                   ? "projective-square-pixels-tight-integer512-bilinear16"
                   : "affine-square-pixels-tight-bilinear16"));
    if (clipped) {
      auto size = Array();
      for (auto extent : track->clips.track_size)
        size.array.push_back(Number(extent));
      Set(&display, "track_size", std::move(size));
      for (const auto& [name, clip] :
           {std::pair{"movie_clip", &track->clips.movie},
            std::pair{"track_clip", &track->clips.track}}) {
        auto raw = Array();
        if (*clip)
          for (auto v :
               {(*clip)->top, (*clip)->left, (*clip)->bottom, (*clip)->right})
            raw.array.push_back(Number(v));
        Set(&display, name, std::move(raw));
      }
    }
    for (const auto& [name, matrix] :
         {std::pair{"movie_matrix", &matrices->movie},
          std::pair{"track_matrix", &matrices->track}}) {
      auto raw = Array();
      for (int32_t value : *matrix) raw.array.push_back(Number(value));
      Set(&display, name, std::move(raw));
    }
  }
  return Failure::kNone;
}
#endif
int Run(int argc, char** argv, bool missing) {
  if (!detail::EnterGrammarWorker(argc, argv)) return 2;
  const rlimit cpu{30, 30};
  if (setrlimit(RLIMIT_CPU, &cpu)) return 2;
  Request request;
  if (!detail::WorkerTransfer(&request, sizeof(request), false) ||
      request.magic != kMagic || !ValidFileVideoLimits(request.limits) ||
      !request.bytes || request.bytes > request.limits.max_source_bytes)
    return 2;
  std::string source(request.bytes, '\0');
  if (!detail::WorkerTransfer(source.data(), source.size(), false)) return 2;
  Ready ready = Ready::kDependency;
  Result result;
  FileVideoPixels pixels;
  int listener = -1;
#ifdef Q4T_HAVE_GSTREAMER_HEADERS
  Runtime api;
  if (!missing && api.Load()) {
    listener = EnterVideoSandbox();
    ready = listener >= 0 ? Ready::kReady : Ready::kSandbox;
  }
#else
  (void)missing;
#endif
  if (!detail::WorkerTransfer(&ready, sizeof(ready), true)) return 3;
  if (ready != Ready::kReady) return 0;
  if (!detail::SendWorkerIoListener(listener)) return 3;
  close(listener);
#ifdef Q4T_HAVE_GSTREAMER_HEADERS
  try {
    result.failure = Decode(api, request, source, &pixels);
  } catch (const std::bad_alloc&) {
    result.failure = Failure::kCapacity;
  } catch (...) {
    result.failure = Failure::kInvalid;
  }
#endif
  const auto metadata = result.failure == Failure::kNone
                            ? OutputJson(pixels.conversion)
                            : std::string{};
  if (result.failure == Failure::kNone) {
    result.metadata_bytes = metadata.size();
    result.frames = pixels.frames.size();
    result.width = pixels.width;
    result.height = pixels.height;
    result.duration_ns = pixels.duration_ns;
    result.scanned_frames = pixels.scanned_frames;
  }
  if (!detail::WorkerTransfer(&result, sizeof(result), true)) return 3;
  if (result.failure != Failure::kNone) return 0;
  if (!detail::WorkerTransfer(const_cast<char*>(metadata.data()),
                              metadata.size(), true))
    return 3;
  for (auto& frame : pixels.frames) {
    video_detail::Frame wire{frame.png.size(), frame.timestamp_ns,
                             frame.source_index, frame.duration_ns};
    if (!detail::WorkerTransfer(&wire, sizeof(wire), true) ||
        !detail::WorkerTransfer(frame.png.data(), frame.png.size(), true))
      return 3;
  }
  return 0;
}
}  // namespace
int MaybeRunFileVideoWorker(int argc, char** argv) {
  if (argc < 2) return -1;
  bool missing = false;
#ifdef Q4T_GRAMMAR_PROCESS_TESTS
  missing = !std::strcmp(argv[1], "--file-video-missing-worker");
#endif
  if (std::strcmp(argv[1], "--file-video-worker") && !missing) return -1;
  try {
    return Run(argc, argv, missing);
  } catch (...) {
    return 3;
  }
}
}  // namespace q4t::server
