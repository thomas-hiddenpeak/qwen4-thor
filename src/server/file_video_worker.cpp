#include "q4t/server/file_video.h"

#include <dlfcn.h>
#include <fcntl.h>
#include <sys/resource.h>
#include <sys/stat.h>
#include <unistd.h>

#include <atomic>
#include <cstring>
#include <mutex>

#include "file_video_protocol.h"
#include "object_protocol.h"
#include "q4t/server/lifetime.h"
#include "q4t/vision/media_plan.h"

#ifdef Q4T_HAVE_GSTREAMER_HEADERS
#include <gst/app/gstappsink.h>
#include <gst/app/gstappsrc.h>
#include <gst/gst.h>
#endif

namespace q4t::server {
namespace {
using namespace object_detail;
using namespace video_detail;
#ifdef Q4T_HAVE_GSTREAMER_HEADERS
struct Runtime {
#define API(name) decltype(&name) name##_fn = nullptr
  API(gst_init_check);
  API(gst_version);
  API(gst_plugin_register_static);
  API(gst_pipeline_new);
  API(gst_element_factory_make);
  API(gst_bin_add);
  API(gst_element_link);
  API(gst_element_set_state);
  API(gst_element_sync_state_with_parent);
  API(gst_element_query_duration);
  API(gst_element_get_bus);
  API(gst_object_unref);
  API(gst_bus_timed_pop_filtered);
  API(gst_mini_object_unref);
  API(gst_element_get_static_pad);
  API(gst_pad_link);
  API(gst_pad_get_current_caps);
  API(gst_caps_get_structure);
  API(gst_structure_get_name);
  API(gst_structure_get_int);
  API(gst_caps_from_string);
  API(gst_sample_get_buffer);
  API(gst_buffer_get_size);
  API(gst_buffer_extract);
  API(gst_buffer_new_allocate);
  API(gst_buffer_fill);
  API(gst_sample_get_segment);
  API(gst_segment_to_stream_time);
  API(gst_app_src_set_callbacks);
  API(gst_app_src_push_buffer);
  API(gst_app_src_end_of_stream);
  API(gst_app_sink_try_pull_sample);
  API(gst_app_sink_is_eos);
  API(g_signal_connect_data);
  API(g_object_set);
  API(g_error_free);
#undef API
  Json modules = Array();
  std::vector<int> pinned;
  ~Runtime() {
    for (int fd : pinned) close(fd);
  }
  void* Library(const std::string& path, const char* name, std::string* sha) {
    int fd = open(path.c_str(), O_RDONLY | O_CLOEXEC);
    if (fd < 0) return nullptr;
    struct stat info {};
    if (fstat(fd, &info) || !S_ISREG(info.st_mode) || info.st_uid != 0 ||
        (info.st_mode & (S_IWGRP | S_IWOTH)) || info.st_size <= 0 ||
        info.st_size > (128LL << 20)) {
      close(fd);
      return nullptr;
    }
    const auto path_fd = "/proc/self/fd/" + std::to_string(fd);
    trace::Digest digest;
    if (!trace::HashFile(path_fd, &digest)) {
      close(fd);
      return nullptr;
    }
    void* handle =
        dlopen(path_fd.c_str(), RTLD_NOW | RTLD_LOCAL | RTLD_NODELETE);
    if (!handle) {
      close(fd);
      return nullptr;
    }
    pinned.push_back(fd);
    *sha = trace::HexDigest(digest);
    (void)name;
    return handle;
  }
  bool Load() {
    // Fixed C kernels avoid runtime executable mappings in the sandbox.
    setenv("ORC_CODE", "backup", 1);
    std::string core_sha, app_sha;
    const std::string root = Q4T_SYSTEM_GSTREAMER_DIRECTORY;
    void* core =
        Library(root + "/libgstreamer-1.0.so.0", "gstreamer", &core_sha);
    void* app = Library(root + "/libgstapp-1.0.so.0", "gstapp", &app_sha);
    if (!core || !app) return false;
#define LOAD(handle, name)                                                 \
  name##_fn = reinterpret_cast<decltype(name##_fn)>(dlsym(handle, #name)); \
  if (!name##_fn) return false
    LOAD(core, gst_init_check);
    LOAD(core, gst_version);
    LOAD(core, gst_plugin_register_static);
    LOAD(core, gst_pipeline_new);
    LOAD(core, gst_element_factory_make);
    LOAD(core, gst_bin_add);
    LOAD(core, gst_element_link);
    LOAD(core, gst_element_set_state);
    LOAD(core, gst_element_sync_state_with_parent);
    LOAD(core, gst_element_query_duration);
    LOAD(core, gst_element_get_bus);
    LOAD(core, gst_object_unref);
    LOAD(core, gst_bus_timed_pop_filtered);
    LOAD(core, gst_mini_object_unref);
    LOAD(core, gst_element_get_static_pad);
    LOAD(core, gst_pad_link);
    LOAD(core, gst_pad_get_current_caps);
    LOAD(core, gst_caps_get_structure);
    LOAD(core, gst_structure_get_name);
    LOAD(core, gst_structure_get_int);
    LOAD(core, gst_caps_from_string);
    LOAD(core, gst_sample_get_buffer);
    LOAD(core, gst_buffer_get_size);
    LOAD(core, gst_buffer_extract);
    LOAD(core, gst_buffer_new_allocate);
    LOAD(core, gst_buffer_fill);
    LOAD(core, gst_sample_get_segment);
    LOAD(core, gst_segment_to_stream_time);
    LOAD(core, g_signal_connect_data);
    LOAD(core, g_object_set);
    LOAD(core, g_error_free);
    LOAD(app, gst_app_src_set_callbacks);
    LOAD(app, gst_app_src_push_buffer);
    LOAD(app, gst_app_src_end_of_stream);
    LOAD(app, gst_app_sink_try_pull_sample);
    LOAD(app, gst_app_sink_is_eos);
#undef LOAD
    // No registry scan, plugin-scanner subprocess, user path or autoplugging.
    setenv("GST_PLUGIN_SYSTEM_PATH_1_0", "", 1);
    setenv("GST_PLUGIN_PATH_1_0", "", 1);
    setenv("GST_REGISTRY", "/q4t-no-video-registry", 1);
    setenv("GST_REGISTRY_UPDATE", "no", 1);
    setenv("GST_REGISTRY_FORK", "no", 1);
    GError* error = nullptr;
    const bool initialized = gst_init_check_fn(nullptr, nullptr, &error);
    if (error) g_error_free_fn(error);
    if (!initialized) return false;
    guint major, minor, micro, nano;
    gst_version_fn(&major, &minor, &micro, &nano);
    if (major != 1 || minor < 20) return false;
    const auto version = std::to_string(major) + '.' + std::to_string(minor) +
                         '.' + std::to_string(micro);
    modules.array.push_back(Object({{"name", String("gstreamer")},
                                    {"version", String(version)},
                                    {"sha256", String(core_sha)}}));
    modules.array.push_back(Object({{"name", String("gstapp")},
                                    {"version", String(version)},
                                    {"sha256", String(app_sha)}}));
    for (const char* name :
         {"coreelements", "app", "isomp4", "matroska", "videoparsersbad",
          "openh264", "vpx", "videoconvertscale", "png"}) {
      std::string sha;
      auto handle =
          Library(root + "/gstreamer-1.0/libgst" + name + ".so", name, &sha);
      if (!handle)
        continue;  // A missing format does not disable other formats.
      const auto symbol = std::string("gst_plugin_") + name + "_get_desc";
      const auto describe = reinterpret_cast<const GstPluginDesc* (*)()>(
          dlsym(handle, symbol.c_str()));
      if (!describe) continue;
      const auto* desc = describe();
      if (!desc || !desc->name || !desc->version ||
          std::strlen(desc->version) > 64 ||
          !gst_plugin_register_static_fn(
              desc->major_version, desc->minor_version, desc->name,
              desc->description, desc->plugin_init, desc->version,
              desc->license, desc->source, desc->package, desc->origin))
        continue;
      modules.array.push_back(Object({{"name", String(name)},
                                      {"version", String(desc->version)},
                                      {"sha256", String(sha)}}));
    }
    // All /proc/self/fd names remained distinct until the last dlopen.
    for (int fd : pinned) close(fd);
    pinned.clear();
    return true;
  }
  void Mini(void* object) {
    if (object)
      gst_mini_object_unref_fn(reinterpret_cast<GstMiniObject*>(object));
  }
};
struct Pipeline {
  Runtime& api;
  const Request& request;
  std::string_view source;
  Pipeline(Runtime& runtime, const Request& options, std::string_view bytes)
      : api(runtime), request(options), source(bytes) {}
  GstElement *pipeline = nullptr, *convert = nullptr;
  std::mutex source_mutex;
  size_t offset = 0;
  std::atomic<Failure> failure{Failure::kNone};
  bool video = false;
  uint32_t width = 0, height = 0;
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
    width = w;
    height = h;
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
  auto* pipeline = api.gst_pipeline_new_fn(nullptr);
  if (!pipeline) return Failure::kCapacity;
  ScopeExit destroy([&] {
    api.gst_element_set_state_fn(pipeline, GST_STATE_NULL);
    api.gst_object_unref_fn(pipeline);
  });
  std::vector<GstElement*> elements;
  for (const char* name :
       {"appsrc", source.substr(4, 4) == "ftyp" ? "qtdemux" : "matroskademux",
        "videoconvert", "capsfilter", "pngenc", "appsink"}) {
    auto* node = api.gst_element_factory_make_fn(name, nullptr);
    if (!node) return Failure::kDependency;
    if (!api.gst_bin_add_fn(reinterpret_cast<GstBin*>(pipeline), node)) {
      api.gst_object_unref_fn(node);
      return Failure::kInvalid;
    }
    elements.push_back(node);
  }
  Pipeline state{api, request, source};
  state.pipeline = pipeline;
  state.convert = elements[2];
  GstAppSrcCallbacks callbacks{};
  callbacks.need_data = Pipeline::Need;
  callbacks.seek_data = Pipeline::Seek;
  api.gst_app_src_set_callbacks_fn(reinterpret_cast<GstAppSrc*>(elements[0]),
                                   &callbacks, &state, nullptr);
  api.g_object_set_fn(
      elements[0], "stream-type", GST_APP_STREAM_TYPE_RANDOM_ACCESS, "format",
      GST_FORMAT_BYTES, "size", static_cast<gint64>(source.size()), "block",
      TRUE, "max-bytes", static_cast<guint64>(131072), nullptr);
  api.g_object_set_fn(elements[5], "sync", FALSE, "max-buffers", guint{1},
                      "drop", FALSE, "wait-on-eos", FALSE, nullptr);
  auto* caps = api.gst_caps_from_string_fn("video/x-raw,format=RGB");
  if (!caps) return Failure::kCapacity;
  api.g_object_set_fn(elements[3], "caps", caps, nullptr);
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
    return Failure::kInvalid;
  uint64_t previous = 0, count = 0, output_bytes = 0;
  FileVideoFrame last;
  for (;;) {
    if (state.failure != Failure::kNone) return state.failure.load();
    auto* sample = api.gst_app_sink_try_pull_sample_fn(
        reinterpret_cast<GstAppSink*>(elements[5]), 100000000);
    if (!sample) {
      auto* message =
          api.gst_bus_timed_pop_filtered_fn(bus, 0, GST_MESSAGE_ERROR);
      if (message) {
        api.Mini(message);
        return state.failure == Failure::kNone ? Failure::kInvalid
                                               : state.failure.load();
      }
      if (api.gst_app_sink_is_eos_fn(
              reinterpret_cast<GstAppSink*>(elements[5])))
        break;
      continue;
    }
    ScopeExit release_sample([&] { api.Mini(sample); });
    auto* buffer = api.gst_sample_get_buffer_fn(sample);
    if (!buffer || !state.width || !state.height) return Failure::kInvalid;
    const auto* segment = api.gst_sample_get_segment_fn(sample);
    const auto timestamp =
        segment ? api.gst_segment_to_stream_time_fn(segment, GST_FORMAT_TIME,
                                                    GST_BUFFER_PTS(buffer))
                : GST_CLOCK_TIME_NONE;
    if (timestamp == GST_CLOCK_TIME_NONE || (count && timestamp <= previous))
      return Failure::kInvalid;
    previous = timestamp;
    if (!count) {
      gint64 duration = 0;
      if (!api.gst_element_query_duration_fn(pipeline, GST_FORMAT_TIME,
                                             &duration) ||
          duration <= 0)
        return Failure::kInvalid;
      if (static_cast<uint64_t>(duration) > request.limits.max_duration_ns)
        return Failure::kCapacity;
      output->duration_ns = duration;
      output->width = state.width;
      output->height = state.height;
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
    if (++count > request.limits.max_scanned_frames ||
        uint64_t(state.width) * state.height >
            request.limits.max_decoded_pixels / count)
      return Failure::kCapacity;
    const auto size = api.gst_buffer_get_size_fn(buffer);
    if (!size || size > request.limits.max_output_bytes)
      return Failure::kCapacity;
    last = {std::string(size, '\0'), timestamp, count - 1, frame_duration};
    if (api.gst_buffer_extract_fn(buffer, 0, last.png.data(), size) != size)
      return Failure::kInvalid;
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
  output->conversion =
      Object({{"format", String("q4t.cpu_video_png.v2")},
              {"source_sha256", String(Digest(source))},
              {"source_bytes", Number(source.size())},
              {"sampling", String("uniform-presentation-time-with-endpoints")},
              {"width", Number(output->width)},
              {"height", Number(output->height)},
              {"duration_ns", Number(output->duration_ns)},
              {"scanned_frames", Number(output->scanned_frames)},
              {"modules", api.modules},
              {"frames", std::move(frames)}});
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
